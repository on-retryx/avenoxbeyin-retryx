"""devices: opt-in, staged multi-device layout for one vault. Off by default.

Beyin stays local-first: nothing here runs unless the user asks for it, and the default
installation is unchanged. Levels:

- `solo`   one computer. The default; nothing is configured.
- `phone`  one computer plus phones/tablets through a file sync service the user already has:
           iCloud Drive (free, Apple devices), Obsidian Sync (paid, any platform) or another
           service. Beyin runs on the computer; the phone reads and edits notes in Obsidian.
- `computers` several computers share the vault through a git remote the user owns. A vault that
           a file sync service also carries keeps its git directory outside the vault, and only
           the computer that registered it (the bridge) runs git (see the section at the end).
           `--sync-once` runs one round: commit, rebase onto the remote, push. A scheduler comes in
           a later step.

An always-on hub is discussed in #205 and not exposed yet.

Rules this module keeps:

- `--detect` and `--plan` write nothing and make no network call. `--apply` writes the settings
  file in the machine-local runtime state and, at the computers level, git metadata (a git
  directory outside the vault, a remote URL, and a managed block in the git directory's
  info/exclude and info/attributes that keeps each machine's installation files out of git);
  it never writes into the vault. `--sync-once`
  talks to the configured remote: that is its whole purpose.
- A rebase that stops on a conflict is aborted, so the working tree (which a phone may be reading
  through a file sync service) never carries conflict markers. Local commits stay; the conflict
  is reported with the exact git command to resolve it by hand.
- Settings live in the runtime state, never in the vault, so they are not synchronised to a
  device they do not describe and a rollback to an older engine ignores them.
- Standard library only.
"""
import calendar
import json
import os
from pathlib import Path
import secrets
import shlex
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

SETTINGS_FILE = 'devices.json'
RECORD_FILE = 'devices-sync.json'
LOCK_FILE = 'devices-sync.lock'
LEVELS = ('solo', 'phone', 'computers')
PHONE_SYNCS = ('icloud', 'obsidian-sync', 'other')
GIT_TIMEOUT = 20
NETWORK_TIMEOUT = 180
OFF_MESSAGE = 'devices sync is off; choose a level with: devices --apply --level computers'
DOC = 'docs/v3/DEVICES.md'
BLOCK_START = '# >>> beyin devices (managed block)'
BLOCK_END = '# <<< beyin devices'
# docs/v3/MULTI-MACHINE.md: every machine installs and updates Beyin itself, so these files carry one
# machine's paths and stay out of git. They go into the git directory's info/exclude, not a .gitignore
# in the vault: nothing is written into a vault that a file sync service carries to a phone.
GITIGNORE_LINES = (
    '.beyin-runtime.json', '.beyin-version', 'beyin.py', 'Beyni Guncelle.cmd',
    'Beyni Güncelle.command', 'Beyni Güncelle.sh', 'Beyni Güncelle.desktop', '.pi/extensions/beyin-v3.ts',
    '.claude/settings.local.json', '.claude/scripts/', '.claude/hermes-plugin/',
    '.claude/skills/beyin/', '.claude/skills/beyin-doktor/', '.claude/skills/beyin-guncelle/',
    '.claude/skills/beyin-kontrol/', '.agents/hooks.json', '.agents/skills/beyin/',
    '.agents/skills/beyin-doktor/', '.agents/skills/beyin-guncelle/', '.agents/skills/beyin-kontrol/',
    '.codex/config.toml', '.codex/hooks.json', '.kiro/hooks/beyin-v3.json', '.kiro/skills/beyin/',
    '.kiro/skills/beyin-doktor/', '.kiro/skills/beyin-guncelle/', '.kiro/skills/beyin-kontrol/',
    '.opencode/plugins/beyin-v3.js', '.omp/hooks/pre/beyin-v3.ts', '**/.beyin-compact.lock',
    '.beyin-note-write/', 'daily/v3/', 'knowledge/v3/', '.DS_Store',
    '.obsidian/workspace*.json',
)
# When the user keeps their own files in .claude/scripts the whole folder must not be ignored.
SCRIPTS_DIR_LINE = '.claude/scripts/'
SCRIPTS_FILE_LINES = ('.claude/scripts/beyin_v3*.py', '.claude/scripts/compile.py',
                      '.claude/scripts/flush.py', '.claude/scripts/__pycache__/')
GITATTRIBUTES_LINES = ('receipts/** -text', '**/Last-Session.md merge=union',
                       '**/Journal.md merge=union', '**/Arşiv/*.md merge=union')
RULE_FILES = (('exclude', 'ignore'), ('attributes', 'attributes'))
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,63}')
BRIDGE_REFUSAL = ('This vault is in a file sync folder. Git sync runs only on the one computer that keeps the '
                  'vault\'s git directory outside the synced folder; register it there with --git-dir PATH. '
                  'Other devices see the notes through the sync service (see ' + DOC + ').')
_SERVICES = (
    ('Library/Mobile Documents/iCloud~md~obsidian/', 'icloud', True),
    ('Library/Mobile Documents/', 'icloud', False),
    ('Library/CloudStorage/Dropbox', 'dropbox', False),
    ('Library/CloudStorage/OneDrive', 'onedrive', False),
    ('Library/CloudStorage/GoogleDrive', 'google-drive', False),
    ('/iCloudDrive/', 'icloud', False),
    ('/Dropbox/', 'dropbox', False),
    ('/OneDrive/', 'onedrive', False),
    ('/OneDrive - ', 'onedrive', False),
    ('/Google Drive/', 'google-drive', False),
)


class DevicesError(ValueError):
    pass


def _atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# ---------------------------------------------------------------- settings

def default_settings():
    return {'schema': 1, 'level': 'solo', 'phone_sync': None, 'git_dir': None, 'remote_name': 'origin',
            'branch': 'main'}


def _valid(data):
    if not isinstance(data, dict) or data.get('schema') != 1:
        return False
    if data.get('level') not in LEVELS or data.get('phone_sync') not in (None, *PHONE_SYNCS):
        return False
    git_dir = data.get('git_dir')
    if git_dir is not None and not (isinstance(git_dir, str) and os.path.isabs(git_dir)):
        return False
    for key in ('remote_name', 'branch'):
        if not (isinstance(data.get(key), str) and _NAME.fullmatch(data[key]) and '..' not in data[key]):
            return False
    if data['level'] == 'solo':
        return data['phone_sync'] is None
    return data['level'] == 'computers' or data['phone_sync'] is not None


def read_settings(state):
    """Return (settings, valid). A missing file is the valid default; a broken file reads as
    the default with valid False and is never overwritten silently."""
    path = Path(state) / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default_settings(), True
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError and broken JSON
        return default_settings(), False
    merged = {**default_settings(), **data} if isinstance(data, dict) else data
    if not _valid(merged):
        return default_settings(), False
    return merged, True


def _read_record(state):
    try:
        data = json.loads((Path(state) / RECORD_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status(vault, state):
    settings, valid = read_settings(state)
    result = {'valid': valid, 'enabled': settings['level'] != 'solo', **settings}
    record = _read_record(state)
    if record:
        result['last_sync'] = record
    return result


def doctor(state):
    """Read-only line for doctor: only the switch while the feature is off."""
    settings, valid = read_settings(state)
    record = _read_record(state)
    if valid and settings['level'] == 'solo' and not record:
        return {'enabled': False}
    result = {'enabled': settings['level'] != 'solo', 'valid': valid, 'level': settings['level'],
              'phone_sync': settings['phone_sync']}
    if settings['level'] == 'computers':
        result['git_dir'] = settings['git_dir']
    if record:
        result['last_sync'] = {key: record.get(key) for key in ('status', 'at', 'reason') if record.get(key) is not None}
        try:
            at = calendar.timegm(time.strptime(record.get('at', ''), '%Y-%m-%dT%H:%M:%SZ'))
            result['last_sync']['age_minutes'] = max(0, int((time.time() - at) // 60))
        except (TypeError, ValueError, OverflowError):
            pass
    return result


# ---------------------------------------------------------------- read-only git facts

def git_dir_args(vault, git_dir):
    """Point git at a git directory kept outside the vault. Passed on every call instead of a
    `.git` file, so nothing machine-specific is written into a synced vault."""
    return ['--git-dir=' + str(git_dir), '--work-tree=' + str(vault)] if git_dir else []


def _git(vault, *args, git_dir=None, timeout=GIT_TIMEOUT):
    exe = shutil.which('git')
    if not exe:
        return subprocess.CompletedProcess(args, 127, '', 'git not found')
    env = dict(os.environ)
    env.update({'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'LANGUAGE': 'C'})
    # Never wait for a passphrase or a host key prompt in an unattended round.
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=20')
    for name in ('GIT_DIR', 'GIT_WORK_TREE'):  # only the explicit arguments decide the repository
        env.pop(name, None)
    argv = [exe, '-C', str(vault), *git_dir_args(vault, git_dir), '-c', 'core.quotePath=false', *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, '', 'timeout after ' + str(timeout) + 's')


def _git_facts(vault):
    """Whether the vault is a git work tree and where its git directory lives. Local commands
    only; remotes are not read."""
    vault = Path(vault).resolve()
    top = _git(vault, 'rev-parse', '--show-toplevel')
    try:
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != vault:
            return {'repository': False}
    except OSError:
        return {'repository': False}
    done = _git(vault, 'rev-parse', '--absolute-git-dir')
    git_dir = Path(done.stdout.strip()).resolve() if done.returncode == 0 and done.stdout.strip() else None
    return {'repository': True,
            'git_dir_inside_vault': bool(git_dir and (git_dir == vault / '.git' or vault in git_dir.parents)),
            # `git init --separate-git-dir` leaves a .git file holding this machine's absolute path.
            'git_file_in_vault': (vault / '.git').is_file()}


# ---------------------------------------------------------------- detection

def sync_location(path):
    """Which file sync service, if any, owns this path. Pure path inspection: no file is read."""
    text = Path(path).as_posix() + '/'
    for marker, service, phone in _SERVICES:
        if marker in text:
            return {'service': service, 'obsidian_ios_folder': phone}
    return {'service': None, 'obsidian_ios_folder': False}


def _obsidian(vault):
    folder = Path(vault) / '.obsidian'
    result = {'vault': folder.is_dir(), 'sync_plugin': False, 'syncthing': (Path(vault) / '.stfolder').exists()}
    try:
        plugins = json.loads((folder / 'core-plugins.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return result
    # Older Obsidian wrote a list of enabled ids, newer versions a {id: bool} object.
    result['sync_plugin'] = ('sync' in plugins if isinstance(plugins, list)
                             else bool(plugins.get('sync')) if isinstance(plugins, dict) else False)
    return result


def detect(vault, state):
    """Describe this machine for level selection. Writes nothing; makes no network call."""
    vault, state = Path(vault), Path(state)
    location = sync_location(vault)
    result = {
        'platform': 'macos' if sys.platform == 'darwin' else 'windows' if sys.platform.startswith('win') else 'linux',
        'python': '.'.join(map(str, sys.version_info[:3])),
        'vault_sync_service': location['service'],
        'vault_in_obsidian_ios_folder': location['obsidian_ios_folder'],
        'state_sync_service': sync_location(state)['service'],
        'obsidian': _obsidian(vault),
        'git': _git_facts(vault),
    }
    result['levels'] = _eligibility(result)
    result['current_fit'] = _current_fit(result)
    return result


def _eligibility(found):
    """What each phone option still needs on this machine. `other` depends on a service Beyin
    cannot inspect, so it lists nothing."""
    phone = {'icloud': [], 'obsidian-sync': [], 'other': []}
    if found['platform'] != 'macos':
        phone['icloud'].append('a Mac that keeps the vault in the Obsidian iCloud folder')
    elif not found['vault_in_obsidian_ios_folder']:
        phone['icloud'].append('move the vault under iCloud Drive/Obsidian (see ' + DOC + ')')
    if not found['obsidian']['sync_plugin']:
        phone['obsidian-sync'].append('an Obsidian Sync subscription and the Sync core plugin turned on in this vault')
    computers = []
    if not shutil.which('git'):
        computers.append('git installed on every computer')
    if found['vault_sync_service'] or found['obsidian']['syncthing']:
        computers.append('this vault is in a file sync folder: only one computer may run git on it, with its git '
                         'directory outside the vault (--git-dir PATH)')
    elif not found['git']['repository']:
        computers.append('a git repository: `git init` in the vault, or --git-dir PATH outside it')
    computers.append('a private remote repository every computer can reach (--remote URL)')
    return {'solo': {'needs': []}, 'phone': {'needs_per_option': phone}, 'computers': {'needs': computers}}


def _current_fit(found):
    """What the environment already looks like, as a starting suggestion and nothing more."""
    phone = ('icloud' if found['vault_in_obsidian_ios_folder'] else
             'obsidian-sync' if found['obsidian']['sync_plugin'] else
             'other' if found['vault_sync_service'] or found['obsidian']['syncthing'] else None)
    return {'level': 'phone' if phone else 'solo', 'phone_sync': phone}


# ---------------------------------------------------------------- options, plan, apply

def normalize(options, state):
    """Validate CLI options against the stored settings; raise DevicesError with a plain reason."""
    stored, valid = read_settings(state)
    if not valid:
        # An explicit --level solo is the way back, also from a file this version cannot read.
        if options.get('level') != 'solo':
            raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid or from a newer version; '
                               'fix or remove it, or reset with --apply --level solo')
        stored = default_settings()
    level = options.get('level') or stored['level']
    if level not in LEVELS:
        raise DevicesError('--level must be one of: ' + ', '.join(LEVELS))
    phone_sync = options.get('phone_sync') if options.get('phone_sync') is not None else stored['phone_sync']
    if phone_sync not in (None, *PHONE_SYNCS):
        raise DevicesError('--phone-sync must be one of: ' + ', '.join(PHONE_SYNCS))
    if level == 'solo':
        if options.get('phone_sync'):
            raise DevicesError('--phone-sync needs --level phone or computers')
        phone_sync = None
    elif level == 'phone' and not phone_sync:
        raise DevicesError('--level phone needs --phone-sync ' + '|'.join(PHONE_SYNCS))
    if level != 'computers':
        for key in ('git_dir', 'remote'):
            if options.get(key):
                raise DevicesError('--' + key.replace('_', '-') + ' belongs to --level computers')
    git_dir = stored['git_dir']
    if options.get('git_dir'):
        given = Path(str(options['git_dir'])).expanduser()
        if not given.is_absolute():
            raise DevicesError('--git-dir must be an absolute path')
        git_dir = str(given.resolve())
    return {**stored, 'schema': 1, 'level': level, 'phone_sync': phone_sync, 'git_dir': git_dir}


def plan(vault, state, options):
    """What `--apply` would store and what is left for the user. Writes nothing."""
    vault, state = Path(vault), Path(state)
    settings = normalize(options, state)
    stored, valid = read_settings(state)
    manual, warnings = [], []
    location = sync_location(vault)
    if sync_location(state)['service']:
        warnings.append('The runtime state is inside a file sync folder. Keep it outside: a synced SQLite index gets corrupted.')

    choice = settings['phone_sync']
    if choice == 'icloud':
        if sys.platform != 'darwin':
            warnings.append('The iCloud bridge is built for a Mac. On other systems iCloud Drive may be missing or keep '
                            'notes as online-only placeholders that Beyin cannot index.')
        if not location['obsidian_ios_folder']:
            manual.append('Move the vault into iCloud Drive/Obsidian so the iPhone or iPad Obsidian app can open it, '
                          'then run the installer with --rebind-state (docs/v3/MULTI-MACHINE.md, moving a vault).')
        manual.append('On the phone: install Obsidian, choose "Open folder as vault" under iCloud Drive/Obsidian. ' +
                      ('Run Beyin on one computer only at this level; the phone reads and edits notes.'
                       if settings['level'] == 'phone' else
                       'This Mac is the only bridge between iCloud and git; other computers use git only.'))
        warnings.append('iCloud also uploads hidden folders. .beyin-note-write/ holds note backups; they stay in your own '
                        'iCloud account. Do not keep the vault in iCloud and Obsidian Sync at the same time.')
    elif choice == 'obsidian-sync':
        if location['service'] == 'icloud':
            warnings.append('This vault is in iCloud. Obsidian advises against combining iCloud with Obsidian Sync; '
                            'pick one of them for this vault.')
        manual.append('In Obsidian on every device: Settings > Core plugins > Sync, sign in and connect the same remote '
                      'vault. Obsidian Sync skips hidden folders other than .obsidian, so each computer keeps its own '
                      'Beyin installation files.')
    elif choice == 'other':
        manual.append('Exclude .beyin-note-write/ and any .git folder from your sync service if it allows exclusions. '
                      'Conflict copies the service creates are reported by `beyin.py sync`, never merged or deleted.')
    facts = _git_facts(vault) if settings['level'] == 'phone' and vault_synced(vault, choice) else {}
    if facts.get('git_dir_inside_vault'):
        warnings.append('This vault is a git repository whose .git folder is inside the synced folder. File sync '
                        'services corrupt git data and carry it to other devices; keep the git directory outside the '
                        'synced folder or stop using git in this vault (see ' + DOC + ').')
    elif facts.get('git_file_in_vault'):
        warnings.append('The vault holds a .git file that points to this machine\'s git directory by absolute path. '
                        'A file sync service carries it to other devices, where that path is wrong (see ' + DOC + ').')
    actions = []
    if settings['level'] == 'computers':
        actions, steps, notes = _computers_plan(vault, settings, options.get('remote'))
        manual.extend(steps)
        warnings.extend(notes)
    return {'level': settings['level'], 'phone_sync': choice, 'manual_steps': manual, 'warnings': warnings,
            'actions': actions, 'settings': settings, 'changes_settings': not valid or settings != stored}


def public_plan(result):
    """The plan as shown to a person."""
    shown = {key: value for key, value in result.items()
             if key != 'settings' and not (key == 'actions' and not value)}
    if shown.get('actions'):
        shown['actions'] = [{key: value for key, value in action.items() if key != 'lines'}
                             for action in shown['actions']]
    return shown


def apply(vault, state, options):
    """Store the chosen level in the runtime state and run the planned git steps. Nothing in the
    vault is written."""
    planned = plan(vault, state, options)
    for action in planned['actions']:
        _execute(vault, planned['settings'], action)
    if planned['changes_settings']:
        _atomic(Path(state) / SETTINGS_FILE, json.dumps(planned['settings'], ensure_ascii=False, indent=2) + '\n')
    changed = planned['changes_settings'] or bool(planned['actions'])
    return {**public_plan(planned), 'status': 'applied' if changed else 'unchanged'}


def _remote_problem(vault, url):
    """Why this remote URL is refused, or None. Nothing is contacted."""
    if not url or url.startswith('-'):
        return 'must not be empty or start with a dash'
    if any(ord(char) < 32 for char in url):
        return 'must not contain control characters'
    parts = urllib.parse.urlsplit(url)
    if parts.password:
        return 'must not contain a password; use an SSH key or a git credential helper'
    local = parts.path if parts.scheme == 'file' else url if os.path.isabs(url) else None
    if local:
        path, vault = Path(local).expanduser().resolve(), Path(vault).resolve()
        if path == vault or vault in path.parents:
            return 'must be outside the vault'
        if sync_location(path)['service']:
            return 'must not be inside a file sync folder: a synced git repository gets corrupted'
    return None


def _computers_plan(vault, settings, remote):
    """Git steps for the computers level: (actions, manual steps, warnings). Reads local git only."""
    vault = Path(vault).resolve()
    git_dir, actions, manual = settings['git_dir'], [], []
    if git_dir:
        problem = git_dir_problem(vault, git_dir)
        if problem:
            raise DevicesError('--git-dir ' + problem)
        if (vault / '.git').exists():
            raise DevicesError('the vault already has a .git entry; Beyin does not move or delete it. Move the '
                               'repository out by hand first, then use --git-dir (see ' + DOC + ').')
        path = Path(git_dir)
        if not is_git_dir(vault, path):
            if path.exists() and (not path.is_dir() or any(path.iterdir())):
                raise DevicesError('--git-dir must be an empty or new folder, or an existing git directory')
            actions.append({'kind': 'init_git_dir', 'summary': 'create the vault\'s git directory outside the vault: '
                            + git_dir, 'git_dir': git_dir})
    elif vault_synced(vault, settings['phone_sync']):
        raise DevicesError(BRIDGE_REFUSAL)
    elif not _git_facts(vault)['repository']:
        raise DevicesError('--level computers needs git: run `git init` in the vault, or keep the git directory '
                           'outside it with --git-dir PATH')
    new_repository = bool(actions)
    rules, notes = _rules_plan(vault, git_dir, new_repository)
    actions.extend(rules)
    name = settings['remote_name']
    current = None
    if not new_repository:
        done = _git(vault, 'remote', 'get-url', name, git_dir=git_dir)
        current = done.stdout.strip() if done.returncode == 0 else None
    if remote:
        problem = _remote_problem(vault, remote)
        if problem:
            raise DevicesError('--remote ' + problem)
        if remote != current:
            actions.append({'kind': 'set_remote', 'summary': ('change' if current else 'add') + ' git remote ' + name
                            + ': ' + remote, 'name': name, 'url': remote, 'replace': bool(current)})
    elif not current:
        manual.append('Connect a private repository that every computer can reach: '
                      + git_command(vault, git_dir, 'remote', 'add', name, 'URL') + ' (or --remote URL).')
    return actions, manual, notes


def _merged_block(existing, lines):
    """Return the file text with the managed block holding every wanted line that is not
    already present outside the block. The rest of the file is left byte for byte."""
    newline = '\r\n' if '\r\n' in existing else '\n'
    rows = existing.splitlines(keepends=True)
    start = end = None
    for index, row in enumerate(rows):
        if row.strip() == BLOCK_START:
            if start is not None:
                raise DevicesError('Duplicate start marker in the managed block.')
            start = index
        elif row.strip() == BLOCK_END:
            if start is None or end is not None:
                raise DevicesError('Unexpected end marker in the managed block.')
            end = index
    if start is not None and end is None:
        raise DevicesError('Missing end marker in the managed block.')
    before = rows if start is None else rows[:start]
    after = [] if end is None else rows[end + 1:]
    kept_outside = {row.strip() for row in (*before, *after)}
    wanted = [line for line in lines if line not in kept_outside]
    prefix = ''.join(before)
    if wanted and start is None and prefix:
        if not prefix.endswith(('\r', '\n')):
            prefix += newline
        if before[-1].strip():
            prefix += newline
    block = newline.join((BLOCK_START, *wanted, BLOCK_END)) + newline if wanted else ''
    return prefix + block + ''.join(after)


def _ignore_lines(vault):
    """The ignore lines for this vault, and the user's own files found in .claude/scripts."""
    scripts = Path(vault) / '.claude/scripts'
    foreign = []
    if scripts.is_dir():
        foreign = sorted(item.name for item in scripts.iterdir()
                         if item.name != '__pycache__' and not item.name.startswith('.')
                         and not re.fullmatch(r'beyin_v3(_[a-z]+)*\.py', item.name)
                         and item.name not in ('compile.py', 'flush.py'))
    if not foreign:
        return list(GITIGNORE_LINES), []
    lines = []
    for line in GITIGNORE_LINES:
        lines.extend(SCRIPTS_FILE_LINES if line == SCRIPTS_DIR_LINE else (line,))
    return lines, foreign


def _info_dir(vault, git_dir):
    """The info folder of the repository Beyin uses: inside the registered external git directory,
    or the ordinary .git folder (its common directory for a linked worktree)."""
    if git_dir:
        return Path(git_dir) / 'info'
    done = _git(vault, 'rev-parse', '--git-common-dir')
    found = done.stdout.strip()
    if done.returncode != 0 or not found:
        raise DevicesError('git could not open the repository: ' + _tail(done.stderr))
    path = Path(found)
    return (path if path.is_absolute() else Path(vault) / path).resolve() / 'info'


def _read_raw(path):
    try:
        with open(path, encoding='utf-8', newline='') as handle:
            return handle.read()
    except FileNotFoundError:
        return ''


def _tracked_machine_files(vault, git_dir):
    paths = [line.rstrip('/') for line in GITIGNORE_LINES if '*' not in line and line != '.DS_Store']
    done = _git(vault, 'ls-files', '--', *paths, git_dir=git_dir, timeout=30)
    return sorted(done.stdout.splitlines())[:40] if done.returncode == 0 else []


def _rules_plan(vault, git_dir, new_repository):
    """Managed-block updates for info/exclude and info/attributes, and warnings. Reads only."""
    ignore, foreign = _ignore_lines(vault)
    notes = []
    if foreign:
        notes.append('.claude/scripts holds your own files (' + ', '.join(foreign[:5]) + '); only Beyin engine '
                     'files are kept out of git there.')
    info = _info_dir(vault, git_dir)
    actions = []
    for name, kind in RULE_FILES:
        lines = ignore if kind == 'ignore' else list(GITATTRIBUTES_LINES)
        existing = '' if new_repository else _read_raw(info / name)
        try:
            merged = _merged_block(existing, lines)
        except DevicesError as exc:
            raise DevicesError(str(exc) + ' Fix it by hand in ' + str(info / name) + '.') from None
        if merged != existing:
            actions.append({'kind': 'git_rules', 'summary': 'keep machine files out of git: managed block in '
                            + str(info / name), 'file': name, 'lines': lines})
    if not new_repository:
        tracked = _tracked_machine_files(vault, git_dir)
        if tracked:
            notes.append('Git already tracks machine-specific installation files: ' + ', '.join(tracked[:8]) +
                         ('...' if len(tracked) > 8 else '') + '. Ignore rules do not untrack them; on one machine run ' +
                         git_command(vault, git_dir, 'rm', '--cached', '-r', '--', *tracked[:8]) +
                         ' and commit (the files stay on disk), see docs/v3/MULTI-MACHINE.md.')
    return actions, notes


def _execute(vault, settings, action):
    git_dir = settings['git_dir']
    if action['kind'] == 'init_git_dir':
        init_external_git(vault, git_dir, settings['branch'])
    elif action['kind'] == 'git_rules':
        # Merged again against the file as it is now: `git init` may have just written a template.
        path = _info_dir(vault, git_dir) / action['file']
        merged = _merged_block(_read_raw(path), action['lines'])
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, merged)
    elif action['kind'] == 'set_remote':
        done = _git(vault, 'remote', 'set-url' if action['replace'] else 'add', action['name'], action['url'],
                    git_dir=git_dir)
        if done.returncode != 0:
            raise DevicesError('git remote failed: ' + (done.stderr or '').strip()[-300:])


# ---------------------------------------------------------------- external git directory
#
# Git and a file sync service must not share a folder: the service corrupts git data, and
# `git init --separate-git-dir` leaves a `.git` file with this machine's absolute path that the
# service carries to every other device. So when the vault is synced, its git directory lives
# outside the vault, its path is kept in the runtime state (devices.json) and every git call
# names it with --git-dir/--work-tree. Only the computer that registered such a directory may
# run git levels on a synced vault: one bridge, the other devices see notes through the service.

def vault_synced(vault, phone_sync=None):
    """Whether a file sync service carries this vault, hidden folders included. Obsidian Sync
    skips hidden folders; iCloud, Syncthing, Dropbox and the like carry them."""
    return bool(phone_sync in ('icloud', 'other') or sync_location(vault)['service'] or
                (Path(vault) / '.stfolder').exists())


def git_dir_problem(vault, git_dir):
    """Why this path cannot hold the vault's git directory, or None."""
    path = Path(str(git_dir)).expanduser()
    if not path.is_absolute():
        return 'must be an absolute path'
    path, vault = path.resolve(), Path(vault).resolve()
    if path == vault or vault in path.parents:
        return 'must be outside the vault'
    if path in vault.parents:
        return 'must not be a folder that contains the vault'
    if sync_location(path)['service']:
        return 'must not be inside a file sync folder: a synced git directory gets corrupted'
    return None


def is_git_dir(vault, git_dir):
    path = Path(git_dir)
    if not path.is_dir():
        return False
    done = _git(vault, 'rev-parse', '--absolute-git-dir', git_dir=path, timeout=GIT_TIMEOUT)
    try:
        return done.returncode == 0 and Path(done.stdout.strip()).resolve() == path.resolve()
    except OSError:
        return False


def init_external_git(vault, git_dir, branch='main'):
    """Create the vault's git directory outside the vault. Writes nothing into the vault: no
    `.git` folder and no `.git` file. An existing git directory at that path is reused."""
    vault = Path(vault).resolve()
    problem = git_dir_problem(vault, git_dir)
    if problem:
        raise DevicesError('--git-dir ' + problem)
    path = Path(str(git_dir)).expanduser().resolve()
    if (vault / '.git').exists():
        raise DevicesError('the vault already has a .git entry; Beyin does not move or delete it. Move the repository '
                           'out by hand first (see ' + DOC + ').')
    if path.exists() and not path.is_dir():
        raise DevicesError('--git-dir exists and is not a folder')
    if path.is_dir() and any(path.iterdir()):
        if is_git_dir(vault, path):
            return {'status': 'unchanged', 'git_dir': str(path)}
        raise DevicesError('--git-dir is a folder that holds other files; choose an empty or new folder')
    path.mkdir(parents=True, exist_ok=True)
    done = _git(vault, 'init', '-q', git_dir=path)
    if done.returncode != 0:
        raise DevicesError('git init failed: ' + (done.stderr or '').strip()[-300:])
    # --initial-branch needs git 2.28; pointing HEAD works on older versions too.
    _git(vault, 'symbolic-ref', 'HEAD', 'refs/heads/' + branch, git_dir=path)
    return {'status': 'created', 'git_dir': str(path)}


def git_command(vault, git_dir, *args):
    """The git command a person can paste into a terminal, aimed at the same repository Beyin uses."""
    parts = ['git', *(git_dir_args(vault, git_dir) or ['-C', str(vault)]), *args]
    return subprocess.list2cmdline(parts) if os.name == 'nt' else shlex.join(parts)


def bridge_git_dir(vault, state):
    """The git directory git levels must use on this machine, or None for an ordinary `.git`
    folder in an unsynced vault. Raises DevicesError when a synced vault has no registered
    external git directory here: that machine is not the bridge and must not run git."""
    vault = Path(vault).resolve()
    settings, valid = read_settings(state)
    if not valid:
        raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    registered = settings.get('git_dir')
    if not registered and not vault_synced(vault, settings.get('phone_sync')):
        return None
    if not registered:
        raise DevicesError(BRIDGE_REFUSAL)
    problem = git_dir_problem(vault, registered)
    if problem:
        raise DevicesError('The registered git directory ' + problem + ': ' + registered)
    if (vault / '.git').exists():
        raise DevicesError('The vault holds a .git entry next to the registered git directory; remove the one in the '
                           'vault by hand so only ' + registered + ' is used (see ' + DOC + ').')
    if not is_git_dir(vault, registered):
        raise DevicesError('The registered git directory is missing or is not a git repository: ' + registered)
    return Path(registered)


# ---------------------------------------------------------------- one sync round

def _utc(seconds=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def _redact(text):
    """Drop credentials embedded in a URL before it is printed or stored."""
    return re.sub(r'(?<=://)[^/@\s]+@', '', text or '')


def _tail(text, limit=300):
    return _redact((text or '').strip())[-limit:]


def _lock(handle):
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _in_progress(git_dir):
    for name, label in (('rebase-merge', 'rebase'), ('rebase-apply', 'rebase'), ('MERGE_HEAD', 'merge'),
                        ('CHERRY_PICK_HEAD', 'cherry-pick'), ('BISECT_LOG', 'bisect')):
        if (Path(git_dir) / name).exists():
            return label
    return None


def _device_label():
    return 'mac' if sys.platform == 'darwin' else 'windows' if sys.platform.startswith('win') else 'linux'


def _round(vault, settings, git_dir):
    """Commit local changes, rebase onto the remote branch, push. git_dir is the registered
    external git directory, or None for an ordinary `.git` folder in an unsynced vault."""
    remote, branch = settings['remote_name'], settings['branch']

    def run(*args, timeout=GIT_TIMEOUT):
        return _git(vault, *args, git_dir=git_dir, timeout=timeout)

    if git_dir is None and not _git_facts(vault)['repository']:
        return {'status': 'error', 'reason': 'the vault is not a git repository'}
    found = run('rev-parse', '--absolute-git-dir')
    if found.returncode != 0 or not found.stdout.strip():
        return {'status': 'error', 'reason': 'git could not open the repository: ' + _tail(found.stderr)}
    repository = Path(found.stdout.strip())
    busy = _in_progress(repository)
    if busy:
        return {'status': 'blocked', 'reason': 'an unfinished ' + busy + ' is open; finish or abort it by hand: ' +
                git_command(vault, git_dir, 'status')}
    current = run('symbolic-ref', '--short', '-q', 'HEAD').stdout.strip()
    if current != branch:
        return {'status': 'blocked', 'reason': 'the vault is on branch "' + (current or 'detached HEAD') +
                '", the sync branch is "' + branch + '"'}
    done = run('add', '-A', timeout=NETWORK_TIMEOUT)
    if done.returncode != 0:
        return {'status': 'error', 'reason': 'git add failed: ' + _tail(done.stderr)}
    committed = False
    if run('diff', '--cached', '--quiet', timeout=60).returncode == 1:
        done = run('commit', '-q', '-m', 'Beyin sync ' + _device_label() + ' ' + time.strftime('%Y-%m-%d %H:%M'),
                   timeout=60)
        if done.returncode != 0:
            return {'status': 'error', 'reason': 'git commit failed (are user.name and user.email set?): ' +
                    _tail(done.stderr)}
        committed = True
    done = run('fetch', '-q', remote, timeout=NETWORK_TIMEOUT)
    if done.returncode != 0:
        return {'status': 'offline', 'committed': committed,
                'reason': 'could not reach remote "' + remote + '": ' + _tail(done.stderr)}
    result = {'committed': committed, 'pulled': False, 'conflicts': []}
    upstream = remote + '/' + branch
    if run('rev-parse', '-q', '--verify', 'refs/remotes/' + upstream).returncode == 0:
        if not run('rev-parse', '-q', '--verify', 'HEAD').stdout.strip():
            done, incoming = run('merge', '-q', '--ff-only', upstream, timeout=60), True  # a new repository joins
        else:
            incoming = run('rev-list', '--count', 'HEAD..' + upstream).stdout.strip() not in ('', '0')
            done = run('rebase', '--autostash', upstream, timeout=NETWORK_TIMEOUT) if incoming else None
        if done is not None and done.returncode != 0:
            conflicts = [line for line in run('diff', '--name-only', '--diff-filter=U').stdout.splitlines() if line]
            if _in_progress(repository):
                run('rebase', '--abort', timeout=60)
            command = git_command(vault, git_dir, 'pull', '--rebase', remote, branch)
            return {**result, 'status': 'conflict', 'conflicts': conflicts[:50], 'resolve_command': command,
                    'reason': ('the same lines changed on two devices; local commits are kept and the remote changes '
                               'were not applied. Resolve the listed files by hand, starting with: ' + command)
                    if conflicts else 'could not apply remote changes: ' + _tail(done.stderr)}
        result['pulled'] = incoming
    head = run('rev-parse', '-q', '--verify', 'HEAD').stdout.strip()
    if not head:
        return {**result, 'status': 'ok', 'pushed': False, 'reason': 'nothing to synchronise yet'}
    if run('rev-parse', '-q', '--verify', 'refs/remotes/' + upstream).stdout.strip() == head:
        return {**result, 'status': 'ok', 'pushed': False, 'head': head[:12]}
    done = run('push', '-q', '-u', remote, branch, timeout=NETWORK_TIMEOUT)
    if done.returncode != 0:
        return {**result, 'status': 'retry', 'pushed': False, 'head': head[:12],
                'reason': 'push was not accepted (another device pushed first, or no write access): ' + _tail(done.stderr)}
    return {**result, 'status': 'ok', 'pushed': True, 'head': head[:12]}


def sync_once(vault, state):
    """One round on this machine. Refuses, writing nothing, while the level has no git or when
    this computer is not the bridge of a synced vault. Records the outcome in the runtime state."""
    vault, state = Path(vault).resolve(), Path(state)
    settings, valid = read_settings(state)
    if not valid:
        raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    if settings['level'] != 'computers':
        raise DevicesError(OFF_MESSAGE)
    git_dir = bridge_git_dir(vault, state)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / LOCK_FILE, 'a+b') as handle:
        if not _lock(handle):
            return {'status': 'already_running'}
        started = time.time()
        try:
            result = _round(vault, settings, git_dir)
        except DevicesError as exc:
            result = {'status': 'error', 'reason': str(exc)}
        result['at'] = _utc(started)
        result['seconds'] = round(time.time() - started, 1)
        _atomic(state / RECORD_FILE, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        return result
