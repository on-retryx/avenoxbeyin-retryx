# Çoklu cihaz düzeni (`devices`)

Beyin yerel önceliklidir ve varsayılan kurulum tek bilgisayar içindir. `beyin.py devices`
komutu, aynı vault'u birden çok cihazda kullanmak isteyenler için **isteğe bağlı ve kademeli**
bir düzenin ilk adımıdır. Komutu hiç çalıştırmazsan hiçbir şey değişmez.

Üç kademe var: tek bilgisayar, bilgisayar + telefon ve git ile birden çok bilgisayar.
Her zaman açık bir merkez makine [#205](https://github.com/avenoxai/avenoxbeyin/issues/205)
altında ayrı bir değişiklik olarak ele alınıyor. Git ile elle eşitlemenin genel kuralları
[MULTI-MACHINE.md](MULTI-MACHINE.md) içindedir.

## Kademeler

| Kademe | Kimin için | Gereken | Ek maliyet |
|---|---|---|---|
| `solo` | Tek bilgisayar. Varsayılan. | Yok | Yok |
| `phone` | Bir bilgisayar + telefon/tablet. Telefonda notları Obsidian ile okur ve düzenlersin; Beyin bilgisayarda çalışır. | Bir dosya eşitleme servisi: iCloud, Obsidian Sync ya da başka biri | iCloud: ücretsiz kotaya sığarsa yok. Obsidian Sync: ücretli abonelik |
| `computers` | Birden çok bilgisayar; her biri kendi Beyin kurulumunu çalıştırır. İstersen telefon da (`--phone-sync`). | Git ve her bilgisayarın erişebildiği özel bir uzak depo | Özel depo çoğu serviste ücretsiz |

`--apply --level solo` her zaman geri dönüş yoludur: `devices.json` bozuksa ya da bu sürümün
tanımadığı bir ayar taşıyorsa (ör. daha yeni bir sürümden geri dönüldüyse) onu da varsayılana çevirir.
Diğer seçenekler böyle bir dosyanın üzerine yazmaz, durur.

### Telefon seçenekleri (`--phone-sync`)

| Değer | Nasıl çalışır | Sınırı |
|---|---|---|
| `icloud` | Vault, Mac'te iCloud Drive içindeki Obsidian klasöründe durur; iPhone/iPad Obsidian aynı klasörü açar. | Apple cihazları ister. Beyin Mac'te çalışır; Mac kapalıyken telefondaki değişiklik Beyin'in indeksine Mac açılınca girer. |
| `obsidian-sync` | Obsidian'ın kendi ücretli eşitlemesi. iOS, Android, Windows, macOS ve Linux arasında çalışır. | Ücretli. Gizli klasörleri (`.obsidian` dışında) eşitlemez; bu yüzden her bilgisayar kendi Beyin kurulum dosyalarını tutar. iCloud klasöründeki bir vault ile birlikte kullanma. |
| `other` | Syncthing, Dropbox, OneDrive gibi başka bir servis. | Servise göre değişir; Beyin yalnız çakışma kopyalarını raporlar. |

`phone` kademesinde Beyin'i **tek bilgisayarda** çalıştır. İki bilgisayar aynı dosya eşitleme
klasöründen Beyin çalıştırırsa kurulum dosyaları birbirinin yollarını taşır ve yeniden kurulum
`Reinstall conflict` ile durur.

## Akış: önce bak, sonra planla, sonra uygula

```sh
python3 beyin.py devices --detect          # bu makine neye hazır; hiçbir şey yazmaz
python3 beyin.py devices --plan  --level phone --phone-sync <seçenek>   # ne kalıyor; hiçbir şey yazmaz
python3 beyin.py devices --apply --level phone --phone-sync <seçenek>   # ayarı kaydeder
python3 beyin.py devices                   # şu anki ayar
python3 beyin.py devices --sync-once       # computers: bir eşitleme turu
python3 beyin.py devices --remove-schedule # computers: zamanlayıcıyı kaldır, kademe kalır
```

Windows'ta `python3` yerine `py -3` kullan.

- `--detect` işletim sistemini, vault'un bir eşitleme klasöründe olup olmadığını, Obsidian ve
  Obsidian Sync izlerini ve vault bir git deposuysa git dizininin nerede olduğunu bildirir
  (uzak depolar okunmaz). `levels` alanı her kademe için eksikleri,
  `current_fit` ortamın şu an neye benzediğini söyler.
- `--plan` çıktısında `manual_steps` senin elle yapman gerekenleri (vault'u taşımak, telefonda
  Obsidian'ı açmak gibi), `warnings` bilmen gereken riskleri listeler.
- `--apply` seçilen kademeyi runtime state içindeki `devices.json` dosyasına yazar.
  `solo` ve `phone` için yaptığı bu kadar: dosya taşımaz, arka plan işi kurmaz.
  `computers` için ayrıca planda listelenen git adımlarını ve istenirse zamanlayıcıyı kurar
  (aşağıda). Vault'a hiçbir kademede dosya yazmaz.

Ajanla kuruyorsan ona şunu söylemen yeter: *"devices --detect çalıştır, bana kademeleri anlat,
seçtiğimin planını göster, onaylarsam uygula."*

## Örnekler

### Mac + iPhone, sunucusuz (iCloud)

```sh
python3 beyin.py devices --apply --level phone --phone-sync icloud
```

Komut ayarı kaydeder ve kalan adımları listeler: vault iCloud Drive/Obsidian klasöründe
değilse oraya taşımak ve kurucuyu `--rebind-state` ile yeniden çalıştırmak
([vault taşıma](MULTI-MACHINE.md#vaultu-başka-klasöre-ya-da-hesaba-taşımak)), telefonda
Obsidian ile klasörü açmak. Runtime state iCloud'a konmaz.

### Her platform + telefon, sunucusuz (Obsidian Sync)

```sh
python3 beyin.py devices --apply --level phone --phone-sync obsidian-sync
```

## Birden çok bilgisayar (`computers`)

Her bilgisayar kendi Beyin kurulumunu çalıştırır ve notlar ortak bir **özel** git deposu
üzerinden eşitlenir. Bir tur şunu yapar: yerel değişiklikleri commit'ler, uzak dalı alır, kendi
commit'lerini onun üstüne taşır (rebase) ve gönderir. Uzak dal `origin/main`'dir.

Kurulum vault'un nerede durduğuna göre iki türlüdür.

### Vault eşitleme klasöründe değilse: her bilgisayarda düz git

```sh
cd <vault> && git init        # vault zaten bir git deposuysa gerekmez
python3 beyin.py devices --plan  --level computers --remote git@github.com:<sen>/<özel-depo>.git
python3 beyin.py devices --apply --level computers --remote git@github.com:<sen>/<özel-depo>.git --interval 15
```

İkinci bilgisayarda aynı depoyu klonla, kendi Beyin kurulumunu yap ve aynı `--apply` komutunu
çalıştır. `--interval` verilmezse zamanlayıcı kurulmaz; işe başlarken ve bitirirken
`devices --sync-once` çalıştır.

### Vault iCloud (ya da başka bir eşitleme) klasöründeyse: tek köprü bilgisayar

Git ile bir dosya eşitleme servisi aynı klasörü paylaşmamalı: servis git verisini bozar ve
`git init --separate-git-dir` düzeninde vault'a yazılan `.git` dosyası bu makinenin mutlak
yolunu diğer cihazlara taşır. Bu yüzden eşitlenen bir vault'ta git **yalnız bir bilgisayarda**
çalışır (köprü; iCloud için bir Mac) ve git dizini vault'un dışında durur:

```sh
python3 beyin.py devices --plan  --level computers --phone-sync icloud \
  --git-dir ~/.local/share/beyin-git/<vault-adı> --remote git@github.com:<sen>/<özel-depo>.git
python3 beyin.py devices --apply --level computers --phone-sync icloud \
  --git-dir ~/.local/share/beyin-git/<vault-adı> --remote git@github.com:<sen>/<özel-depo>.git --interval 15
```

- `--git-dir` mutlak yol olmalı; vault'un ve herhangi bir eşitleme klasörünün dışında, boş ya da
  yeni bir klasör ya da var olan bir git dizini olmalı. Yolu yalnız runtime state'teki
  `devices.json` tutar; vault'a `.git` dosyası ya da klasörü yazılmaz. Beyin her git çağrısında
  `--git-dir=… --work-tree=…` verir.
- Vault'ta zaten bir `.git` varsa Beyin onu taşımaz ve silmez; komut durur. Depoyu önce elle
  vault'un dışına taşı, sonra `--git-dir` ile kaydet.
- Diğer bilgisayarlar ve telefon notları eşitleme servisi üzerinden görür. Eşitlenen bir vault'ta
  kayıtlı dış git dizini olmayan bir makinede `--apply --level computers` ve `--sync-once`
  hiçbir şey yazmadan reddedilir.
- macOS'ta arka plan işleri iCloud Drive'ı okuyamaz: zamanlayıcı kurulurken plan, bu Python'a
  Sistem Ayarları > Gizlilik ve Güvenlik > Tam Disk Erişimi vermeni söyler; vermezsen zamanlanmış
  tur "Operation not permitted" ile düşer.

### Git kuralları

`--apply`, git dizininin `info/exclude` ve `info/attributes` dosyalarına yönetilen bir blok
yazar (vault'a `.gitignore` yazmaz). `exclude` her makinenin kendi yollarını taşıyan Beyin kurulum
dosyalarını git dışında tutar ([MULTI-MACHINE.md](MULTI-MACHINE.md)); `attributes` receipt'leri
olduğu gibi saklar ve `Last-Session.md`, `Journal.md` ile arşiv notlarında iki cihazın eklediği
satırları birleştirir (`merge=union`). Bloğun dışındaki satırlara dokunulmaz. Git bu kurulum
dosyalarını zaten izliyorsa plan, onları izlemeden çıkaran tam komutu yazar.

### Tur sonucu

`--sync-once` sonucu runtime state'teki `devices-sync.json` dosyasına yazar; `devices` ve
`doctor` son turu gösterir.

| Durum | Anlamı |
|---|---|
| `ok` | Değişiklikler alındı ve/veya gönderildi (ya da yapacak iş yoktu). |
| `offline` | Uzak depoya ulaşılamadı; yerel commit duruyor, sonraki tur yeniden dener. |
| `retry` | Gönderim kabul edilmedi (başka cihaz önce gönderdi ya da yazma izni yok); sonraki tur yeniden dener. |
| `conflict` | Aynı satırlar iki cihazda değişti. Yerel commit'ler korunur, uzak değişiklik uygulanmaz, vault'ta çakışma işareti bırakılmaz. Çıktıdaki `resolve_command` kopyalanıp çalıştırılabilecek tam git komutudur. |
| `blocked` | Yarım kalmış bir rebase/merge açık ya da vault eşitleme dalında değil; çıktıdaki komutla elle bitir. |
| `already_running` | Bu vault'ta başka bir tur sürüyor. |

Çakışmada çıktı, köprüde de çalışan tam komutu verir, ör.
`git --git-dir=/Users/sen/.local/share/beyin-git/kasa --work-tree=/Users/sen/…/kasa pull --rebase origin main`.
Bu komutla çakışan dosyaları düzelt, `git … add` ve `git … rebase --continue` ile bitir; sonraki
tur gönderir.

### Zamanlayıcı

`--interval N` (5..1440 dakika) kullanıcı başına bir zamanlayıcı kurar: macOS'ta launchd, Linux'ta
systemd kullanıcı zamanlayıcısı, Windows'ta Görev Zamanlayıcı. Bunlar yoksa (ör. bir konteynerde)
plan, cron satırını ya da elle çalıştırılacak komutu yazar. Zamanlayıcının adı vault yolunun kısa
bir özetini taşır; farklı yollardaki iki kurulum birbirinin zamanlayıcısına dokunmaz. Beyin yazdığı
her dosyayı içerik özetiyle `devices-schedulers.json` içine kaydeder ve kaldırırken yalnız kendi
yazdığı, değişmemiş dosyaları siler.

Zamanlayıcıyı kaldırmak için `--remove-schedule`, `--interval 0` ya da `solo`/`phone`'a dönüş
yeter. `devices` modülü olmayan bir sürüme `rollback` ve `uninstall` da kayıtlı zamanlayıcıyı
kaldırır; kademe ayarı kalır, yeni sürüme dönünce `--apply … --interval N` ile yeniden kurulur.

## Git deposu olan vault

Plan iki durumu uyarı olarak yazar; komut git'e dokunmaz:

- `.git` klasörü vault'un içinde: dosya eşitleme servisleri git verisini bozabilir ve onu diğer
  cihazlara taşır.
- `.git` bir dosya (`git init --separate-git-dir` düzeni): içinde bu makinenin git dizininin
  mutlak yolu yazar; eşitleme servisi onu, yolun yanlış olduğu diğer cihazlara taşır.

Uyarı `icloud` ve `other` seçeneklerinde, vault bir eşitleme klasöründeyse ya da Syncthing izi
(`.stfolder`) varsa verilir. Obsidian Sync gizli klasörleri eşitlemediği için tek başına uyarı
gerektirmez. Git dizinini eşitlenen klasörün dışında tut (`computers` kademesinde `--git-dir`)
ya da bu vault'ta git kullanma.

## Neye dokunur, neye dokunmaz

- Ayar (`devices.json`) **state klasöründedir**; vault'a ve dolayısıyla diğer cihazlara gitmez.
  Bu komutu içermeyen eski bir motor sürümüne geri dönersen dosya yok sayılır.
- Vault'a hiçbir dosya yazmaz; notları taşımaz, silmez, yeniden adlandırmaz. Vault'u eşitleme
  klasörüne taşımak senin adımındır. `computers` turu notlarını commit'ler ve uzak değişiklikleri
  uygular; bu git'in kendi işidir.
- `computers` için yazılanlar: dış git dizini (`--git-dir`), git dizininin `info/exclude` ve
  `info/attributes` blokları, `git remote`, kullanıcı zamanlayıcısı ve state'teki
  `devices-sync.json`, `devices-schedulers.json`.
- `--detect`, `--plan` ve durum çıktısı ağa çıkmaz; git için yalnız yerel komutlar çalışır.
  Ağa yalnız `--sync-once` çıkar (`fetch` ve `push`).
- `doctor` kademe açıkken bir satırla gösterir; kapalıyken yalnız `{"enabled": false}` yazar.

## Gerçek cihazda denenenler

| Parça | Durum |
|---|---|
| macOS (Python 3.14), vault iCloud Drive/Obsidian klasöründe: `--detect`, `--plan`, `--apply --level phone --phone-sync icloud`, `doctor`, `--apply --level solo` | Bu sürümle denendi |
| `git init --separate-git-dir` ile kurulmuş kasada `.git` dosyası algılama ve uyarısı | Bu sürümle denendi |
| iPhone'da Obsidian ile aynı iCloud klasörü | Denendi (bu sürüm vault'a yazmadığı için telefon tarafı değişmez) |
| `obsidian-sync` ve `other` telefon seçenekleri | Denenmedi |
| Windows ve Linux'ta `--detect` | Birim testleriyle sınandı, gerçek makinede denenmedi |
| `computers`: dış git dizinli köprü Mac, düz git'li ikinci bilgisayar, çevrimdışı çalışma ve çakışma kurtarma | Geçici depolarla uçtan uca testlerde sınandı, gerçek cihazda denenmedi |
| `computers` zamanlayıcısı (launchd, systemd, Görev Zamanlayıcı) | Birim testleriyle sınandı, gerçek makinede denenmedi |

## Bilinen sınırlar

- Bu düzen dosya düzeyinde eşitlemedir; cihazlar arası kayıt düzeyinde birleştirme, cihaz
  yetkilendirmesi ve onay protokolü #205 altında açık tasarım işidir.
- iCloud gizli klasörleri de yükler; `.beyin-note-write/` altındaki not yedekleri kendi iCloud
  hesabında durur.
