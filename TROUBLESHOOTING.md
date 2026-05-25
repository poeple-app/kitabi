# Kitabi Kurulum — Sorun Giderme Rehberi

Bu rehber kurulumun her adımında karşılaşabileceğin sorunlar için. Baştan sona okumana gerek yok — kurulumda hangi adımda takıldıysan sadece o bölüme bak.

İçindekiler ile aradığın bölüme atlayabilirsin.

📧 **İletişim**: poeple.api@gmail.com — burada cevap bulamadığın sorun olursa.

---

## İçindekiler

- [Genel kurulum öncesi sorular](#genel-kurulum-oncesi)
- [Adım 1: Telegram bot oluşturma](#adim-1-telegram)
- [Adım 2: Gemini API anahtarı](#adim-2-gemini)
- [Adım 3: GitHub'da fork](#adim-3-github)
- [Adım 4: Google Cloud Project](#adim-4-gcp)
- [Adım 5: Secret Manager](#adim-5-secrets)
- [Adım 6: Cloud Run'a deploy](#adim-6-deploy)
- [Adım 7: Webhook bağlantısı](#adim-7-webhook)
- [Bot çalışmıyor / mesajıma cevap vermiyor](#bot-cevap-vermiyor)
- [Veri kaybı, log'lar, izinler](#genel-sorunlar)

---

## Genel kurulum öncesi sorular {#genel-kurulum-oncesi}

### Hiç kod bilmiyorum, bunu kullanabilir miyim?

Evet. Kurulum sihirbazı seni tüm adımlardan geçirir, sen sadece tıklamalar yapıp yapıştırmalar yapacaksın. Bu rehber de takıldığın yerleri çözmek için. Linux/terminal/komut bilgisi gerekmiyor.

### Gerçekten 0 TL mi tutar?

Evet. Kullanılan tüm servisler ücretsiz katmanlarda — Telegram, Gemini, Google Cloud Run, Cloud Storage, Secret Manager, GitHub. Kitabi'nin kişisel kullanım yükü bu limitlerin çok altında. Detaylar kurulum sihirbazının ilk sayfasında.

### Ne kadar sürer?

İlk kez yapıyorsan ~30-45 dakika. Yarın güncelleme yaparsan ~2 dakika (sadece kod push'la).

### Google hesabım yok, ne yapayım?

[accounts.google.com/signup](https://accounts.google.com/signup) → Gmail oluştur. Telefon numarası istenir, doğrulama SMS'i gelir. ~3 dakika. Sonra Kitabi kurulumuna döner, "Gmail ile giriş yap" tüm adımlarda aynı hesabı kullanırsın.

### Telegram hesabım yok

Telefonun var mı? [telegram.org](https://telegram.org) → uygulamayı indir → telefon numaranı gir → SMS kodu → tamam. Kullanıcı adı seçmen gerekmiyor.

### Mac/Windows fark eder mi?

Hayır. Tüm kurulum tarayıcı içinde yapılıyor. İşletim sisteminin önemi yok.

### Telefonda kurulum yapabilir miyim?

Teorik olarak evet (sihirbaz mobile responsive), ama bilgisayar daha kolay — birden çok sekme açacaksın, kopyala-yapıştır yapacaksın.

---

## Adım 1: Telegram bot oluşturma {#adim-1-telegram}

### BotFather'ı bulamıyorum

Telegram'ı aç → üstteki arama çubuğuna `@BotFather` yaz → mavi tikli olanı seç (verified). Sahte taklitleri var — mavi tik şart.

Direkt link: [t.me/BotFather](https://t.me/BotFather)

### `/newbot` yazınca cevap gelmedi

BotFather'a "Start" demediysen, önce alttaki **START** butonuna bas. Sohbet başladıktan sonra `/newbot` çalışır.

### Bot adı kabul edilmiyor

İlk soruda BotFather "name" istiyor — bu **görünür ad**. İstediğin karakterleri yazabilirsin (Türkçe karakter dahil): "Kitabi", "Okuma Botu", "Benim Kitabim"...

### Username kabul edilmiyor — "Sorry, this username is already taken"

Username'ler global benzersiz; `kitabi_bot`, `kitabi_app_bot` gibi popüler isimler dolu olabilir. Sonuna sayı veya kendine özgü bir kelime ekle:
- `kitabi_okuma_2026_bot`
- `kitabi_faruk_bot`
- `benim_kitabim_okuma_bot`

Kurallar: harf+rakam+`_`, 5-32 karakter, **mutlaka `bot` ile bitmeli**.

### Token nedir, nasıl saklayacağım?

BotFather'ın verdiği `123456789:ABCdefGhIjKlMnOpQrStUvWxYz` şeklindeki uzun yazı **bot token'ı**. Bu **botun şifresi** — kim onu alırsa botun sahibi gibi davranabilir.

**Yapacakların:**
1. Token'ı seç, kopyala
2. Notepad / TextEdit aç, yapıştır, kaydet (geçici)
3. Adım 5'te Google Cloud Secret Manager'a yapıştır
4. Notepad'i sil

**Yapmaman gerekenler:**
- Tokeni e-postaya, mesaja, social media'ya yazma
- Kod dosyalarına yapıştırma
- Screenshot alıp herhangi bir yere göndererek paylaşma

### Token'ı kaybettim

Sorun değil. BotFather'da `/token` yaz → bot'unu seç → yeniden gösterir. Eski token iptal olmaz, yeni de aynı token döner. Eğer "yeni token istiyorum, eskisi sızdı" diyorsan `/revoke` ile iptal edip yeni alabilirsin.

### Birden fazla bot mu yarattım?

Sorun değil. BotFather'a `/mybots` yaz, listele. Kullanacağını seç, gerisi de durabilir (sınır yok ama temiz tutmak istersen `/deletebot`).

### userinfobot ID'mi söylemiyor

[@userinfobot](https://t.me/userinfobot) → START → otomatik ID'ni yazar. Eğer yazmazsa herhangi bir mesaj yaz, cevap olarak gelir.

**Önemli:** Telegram **username**'ini değil, **ID**'ni (sadece rakamlardan oluşan numara) kaydedeceksin.

---

## Adım 2: Gemini API anahtarı {#adim-2-gemini}

### Google AI Studio sayfası açılmıyor

URL: [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey)

Açılmıyorsa:
- Google hesabınla giriş yaptın mı? Sağ üstte profil resmi görünmeli
- Ülke kısıtı olabilir — Google AI bazı ülkelerde kısıtlı. VPN ile (örn. Almanya) dene
- Tarayıcıyı yenile, cache temizle

### "Create API Key" butonu gri / tıklanmıyor

İki sebep var:
1. Bir Google Cloud Project'in yok — "Create API key in new project" seçeneğini bul, kabul et. Otomatik bir project açar.
2. Hesabın yeni; Google bazen birkaç saat onaylama bekler. Tekrar dene.

### API key formatı `AIza...` ile başlamıyor

Bu yanlış key olabilir. API key 39 karakter uzunluğunda, `AIzaSy...` ile başlar. Tekrar oluştur, doğru olanı kopyala.

### API key kaybettim

AI Studio → API keys → varolan anahtarın yanında "Show key" tıkla, görünür. Kaybettiğin diye yeni oluşturmana gerek yok.

### Quota aşıldı diyor (429 hata)

Gemini ücretsiz limit: 10 istek/dakika, 250/gün.
- Saat veya gün sınırına geldiysen bekle
- Veya başka bir API key (başka Google hesabıyla) oluştur — pratikte zor olur, kişisel kullanımda 250 RPD'yi geçmek zor

### Gemini cevap vermiyor / "API key invalid"

Önce key'i kopyaladığından emin ol — başında/sonunda boşluk olmamalı.
Sonra `https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY` URL'sini tarayıcıda aç (KEY yerini sen koy). Liste dönerse key çalışıyor; "API key not valid" dönerse key yanlış.

---

## Adım 3: GitHub'da fork {#adim-3-github}

### GitHub hesabım yok

[github.com/signup](https://github.com/signup) → e-mail + username + şifre → mail doğrulama → tamam. 2 dakika. Kredi kartı yok.

### Doğrulama maili gelmedi

Spam klasörüne bak. Hâlâ yoksa `Resend verification email` butonu var.

### Fork butonunu bulamıyorum

Repo sayfasının sağ üst köşesinde **"Fork"** yazılı bir buton vardır (yıldız ve "Watch"un yanında).

Direkt fork URL: [github.com/poeple-app/kitabi/fork](https://github.com/poeple-app/kitabi/fork) — bu link doğrudan fork ekranını açar.

### "Authorize Cloud Run" mesajı çıktı

Bu Cloud Run'ın senin GitHub repolarını okuma izni istemesi (sadece public olanları zaten okuyabiliyor, ama bu izin private repoları da kapsıyor). Güvenli, "Authorize" ile devam et.

### Fork ettim ama URL'ini bulamıyorum

GitHub → profilin → repos sekmesi → "kitabi" listede. URL'i `https://github.com/<kullaniciAdin>/kitabi` şeklinde. Kurulumda bu URL'i (sondaki `/fork` olmadan!) gireceksin.

### "Repository name 'kitabi' already exists in your account"

Daha önce forklamışsın. İki seçenek:
1. Eski fork'u sil (Settings → Danger Zone → Delete) ve tekrar fork yap
2. Eski fork'u kullan; ama güncel kodu çekmek için repo sayfasında "Sync fork" yap

### Repo gözükmüyor / private

Default'ta fork public olur. Eğer sehven private yaptıysan, deploy.cloud.run repoya erişemez. Repo settings → General → Visibility → "Make public".

---

## Adım 4: Google Cloud Project {#adim-4-gcp}

### Google Cloud Console açılmıyor

[console.cloud.google.com](https://console.cloud.google.com) — Gmail ile giriş yap.

Açılmıyorsa:
- Çıkış yapıp yeniden gir
- Farklı tarayıcı dene
- Adblocker'ı bu site için kapat

### Hizmet şartları kabul etmem isteniyor

İlk girişte normal. Kabul et, devam et. Ücret yok.

### Project oluşturmak istemiyor — "create a new project" yok

Sağ üstte proje seçici (logo'nun yanında "Select a project" yazıyor) → tıkla → açılan pencerede sağ üstte **"NEW PROJECT"**.

### Kredi kartı isteniyor — para çekecek mi?

Hayır. Google Cloud'un free tier'ı "trial" değil, "always free". Kart sadece kimlik doğrulama için (bot yapımı engellemek için). Kitabi'nin kullanımı ücretsiz limit içinde, kart'tan ödeme alınmaz.

**Eğer kart vermek istemiyorsan:**
- "Skip" / "Maybe later" seçeneği bazen var, "Cancel" değil
- Kart vermeden de Cloud Run / Storage / Secret Manager ücretsiz limitlerde kullanılabilir, ama bazı hesaplarda zorunlu
- Alternatif: prepaid (önödemeli) kart ile aç

### Project ID nedir?

Project oluştururken sen "Project name" giriyorsun (örn. "Kitabi"). Google otomatik bir **Project ID** üretiyor (örn. `kitabi-487291`). ID benzersiz ve değiştirilemez.

Project ID'i bulmak için: Console üst kısımda project adının yanında küçük yazıyla görünür. Veya **Dashboard** → "Project info" kutusu.

### API'leri aktifleştiremiyorum

API enable sayfası: `console.cloud.google.com/apis/library`. Tek tek arama yapıp her birini "Enable" yapabilirsin, veya kurulumdaki "4 API'yi tek tıkla aç" linki dördünü birden açar.

İsim listesi (manuel yapacaksan):
- Secret Manager API
- Cloud Run Admin API
- Cloud Storage API
- Artifact Registry API

### "Billing account required"

Bazı API'ler billing account (kart) bağlı bir project gerektirir. Yukarıdaki "Kredi kartı isteniyor" bölümüne bak.

### Çok project'im var, hangisi kullanılıyor?

Console'un üst kısmında aktif project'in adı yazıyor. Kurulumda Project ID girerken aktif project'i yazıyorsun. Yanlış project'i seçtiysen sağ üstten değiştir.

---

## Adım 5: Secret Manager {#adim-5-secrets}

### Secret Manager menüsünde göremiyorum

Console sol menüde "Security" altında. Yoksa:
- API'leri aktifleştirmedin (Adım 4) — geri dön, Secret Manager API'yi etkinleştir
- Project değiştir (üstte aktif project'in adı kontrol et)

Direkt link (Project ID girersen): `console.cloud.google.com/security/secret-manager?project=<PROJECT_ID>`

### "Permission denied"

Project sahibi sensin değil mi? Eğer ortak project'tesen, Owner / Editor / Secret Manager Admin rolü gerek. Kendi project'inse normalde tüm yetkiler sende olur.

### "Create Secret" formu açılmıyor

Adblocker veya privacy uzantısı engelliyor olabilir. Adblocker'ı kapat.

### Secret değer alanı boş gözüküyor

Form scroll'lanması gerek. "Secret value" başlığı altında büyük bir textarea var. Eğer hâlâ göremiyorsan farklı tarayıcı dene.

### Secret adını yanlış mı yazdım?

Adlar **tam olarak şu olmalı** (büyük-küçük harf hassas):
- `telegram-bot-token`
- `allowed-tg-user-ids`
- `gemini-api-key`
- `webhook-secret`

Yanlış yazdıysan Secret Manager'da o secret'ı seç → "DESTROY" → tekrar oluştur. Veya doğru adla yeni bir secret oluştur ve eskisini görmezden gel.

### Webhook secret üreteci çalışmıyor

Kurulum sayfası tarayıcının `crypto.getRandomValues` API'sini kullanıyor. Modern tarayıcılarda çalışır. Eski tarayıcı kullanıyorsan güncelle.

Alternatif olarak terminal'inde manuel üretebilirsin:
- macOS/Linux: `openssl rand -hex 32`
- Windows PowerShell: `$bytes = New-Object byte[] 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes); ([System.BitConverter]::ToString($bytes) -replace '-','').ToLower()`

### Service account'un izni nasıl?

Eğer veri kaydedilmiyor uyarısı gelirse: Cloud Run service account'unun GCS bucket'a yazma yetkisi olmalı.

```
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:<PROJECT_ID>-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

Cloud Shell'de bu komutu çalıştır.

---

## Adım 6: Cloud Run'a deploy {#adim-6-deploy}

### Deploy sayfası açılmıyor

[deploy.cloud.run/?git_repo=<REPO_URL>](https://deploy.cloud.run) — repo URL'ini parametre olarak almalı. Kurulum sihirbazı bunu otomatik üretir.

Açılmıyorsa: Console'da [Cloud Run](https://console.cloud.google.com/run) → "Create service" → "Continuously deploy from a repository" → GitHub provider seç.

### Build başarısız oldu — "FAILED" görüyorum

Build log'larına bak (Cloud Run service → "BUILD LOGS"). Yaygın sorunlar:
- **Dockerfile yok**: repo'da `Dockerfile` olmalı (varolan kodda var; eğer kendi fork'un eskiyse Sync Fork yap)
- **Bağımlılık çakışması**: `pip install` aşamasında patladıysa `pyproject.toml`'da bir paket bozulmuş — issue aç
- **Memory yetmedi**: build için varsayılan 2 GB yetmez bazen. Build settings → "Compute" → "8 GB" gibi yüksek seç (sadece build için, runtime memory ayrı)

### Authentication "Allow unauthenticated invocations" göremiyorum

Deploy sırasında "Authentication" bölümünde iki seçenek var:
- Allow unauthenticated invocations
- Require authentication

Birincisini seç. Webhook için gerekli; kötü amaçlı erişimi `webhook-secret` engelliyor.

### Secret bağlama (Reference a secret) nasıl yapılır?

Deploy sayfası → "Variables & Secrets" bölümü → "Reference a Secret" butonu.

Her secret için:
1. Reference a Secret → seç
2. Environment variable name (sol kolon): `TELEGRAM_BOT_TOKEN` (büyük harf, alt çizgi)
3. Secret (sağ kolon): `telegram-bot-token` (küçük harf, tire)
4. Version: `latest`
5. "Done"

Aynısını şu 4 secret için tekrarla:
- `TELEGRAM_BOT_TOKEN` ← `telegram-bot-token:latest`
- `WEBHOOK_SECRET` ← `webhook-secret:latest`
- `ALLOWED_TG_USER_IDS` ← `allowed-tg-user-ids:latest`
- `GEMINI_API_KEY` ← `gemini-api-key:latest`

Ayrıca düz env vars:
- `GCS_BUCKET_NAME` ← (sen oluşturduğun bucket adı)
- `BOT_BASE_URL` ← Cloud Run servisi deploy'dan sonra verdiği URL — ilk deploy'da boş bırak, sonra update yap

### Deploy bitti ama URL gelmedi

Service detail sayfasında üstte "URL" yazısı var (`https://kitabi-xxxxx-ew.a.run.app` gibi). Eğer yoksa deploy henüz bitmemiş veya hata almış.

### Cold start çok uzun (>30 sn)

İlk açılışta normal — container indiriliyor, Python paketleri yükleniyor, DB bağlanıyor. Sonraki istek hızlı olur (~1-2 sn).

Eğer her seferinde uzun: `min-instances=1` yapabilirsin (cold start tamamen yok ama ücretsiz limit aşılabilir).

### Servis URL'i değişti

Deploy ile her revision yeni URL döndürebilir (eski URL de çalışır). Eski URL'i `BOT_BASE_URL`'de tutuyorsan webhook patlar.

---

## Adım 7: Webhook bağlantısı {#adim-7-webhook}

### Cloud Shell açılmıyor

[shell.cloud.google.com](https://shell.cloud.google.com) → ilk açılışta "Authorize" → home directory oluşturur.

Açılmıyorsa farklı tarayıcı veya inkognito dene.

### Cloud Shell'de curl komutu hata verdi

Yaygın hatalar:
- `command not found: gcloud` — Cloud Shell'de gcloud zaten var; eğer yoksa "Open new tab in new browser tab" yap, taze shell aç
- `Permission denied` — secret'a erişimin yok; Cloud Shell account'un Secret Manager izni gerek (varsayılan olarak var)
- `Failed to read secret` — secret adı yanlış; kontrol et

### Komut çıkışı `{"ok":true}` değil

Telegram API her zaman `{"ok":true}` veya `{"ok":false, "description":"..."}` döner. `false` ise description sebep:
- "Bad webhook" — URL yanlış (Cloud Run URL'ini doğru yapıştırdın mı?)
- "Wrong response from the webhook" — webhook URL'i Cloud Run'da çalışmıyor olabilir; servis ayakta mı?

### Log'da "webhook set" göremedim (Yöntem B)

Cloud Run logs → severity filter "INFO ve üstü" → "webhook" ara.

Hiç çıktı yoksa:
- `BOT_BASE_URL` env'i set değil — Cloud Run service → "EDIT & DEPLOY NEW REVISION" → Variables → `BOT_BASE_URL=<senin Cloud Run URL>`
- `TELEGRAM_BOT_TOKEN` secret'ı yanlış
- Service başlangıçta hata atmış; loglarda errortrace'i ara

### Webhook kuruldu ama bot cevap vermiyor

Aşağıdaki "Bot mesajıma cevap vermiyor" bölümüne git.

---

## Bot çalışmıyor / mesajıma cevap vermiyor {#bot-cevap-vermiyor}

### Önce kontrol: Telegram'da kendi botunla konuşuyor musun?

Senin yarattığın botun username'ine git (örn. `@kitabi_okuma_bot`) → "Start" → `/start` yaz.

Başka bir kullanıcıya mesaj atıyorsan o cevap vermez (allowlist sadece sen).

### "User not allowed" log'u var

`ALLOWED_TG_USER_IDS` secret'ı yanlış. Sadece **rakamlar** olmalı (`123456789`), virgülle birden fazla olabilir (`123,456`). Username yazma, telefon numarası yazma — Telegram **user ID** olmalı.

ID'i öğrenmek için [@userinfobot](https://t.me/userinfobot)'a `/start` at.

### Webhook URL'i set edilmemiş

Cloud Shell'de:

```
TG_TOKEN=$(gcloud secrets versions access latest --secret=telegram-bot-token)
curl "https://api.telegram.org/bot$TG_TOKEN/getWebhookInfo"
```

`"url":""` görüyorsan webhook hiç set edilmemiş. Adım 7'yi tekrar yap.
`"url":"https://kitabi-xxx.run.app/webhook"` görüyorsan set edilmiş ama farklı sorun var.

### Cloud Run servisinde hata var

Logs Explorer → severity ERROR → son saat.

En sık hatalar:
- `Settings validation error` — secret eksik
- `Database is locked` — eşzamanlı yazma çakışması (yeniden başlat: service → edit → save)
- `GeminiCallFailed` — Gemini quota / API key sorunu

Hata mesajını GitHub Issues'a yapıştırırsan yardımcı olurum.

### "Bad webhook: Wrong response from the webhook"

Webhook URL'in 200 dönüyor olmalı. Test et:

```
curl https://<senin-cloud-run-url>/healthz
```

`{"status":"ok"}` dönmüyorsa servis düzgün ayakta değil.

### "Bad webhook: HTTP URL is forbidden"

Webhook **HTTPS** olmalı (HTTP değil). Cloud Run zaten HTTPS verir, eğer http://...run.app yazdıysan **s**'yi unutmuşsun.

---

## Veri kaybı, log'lar, izinler {#genel-sorunlar}

### Veri kayboldu (notlar görünmüyor)

İki olası sebep:
1. **GCS bucket bağlı değil**: `GCS_BUCKET_NAME` env var'ı boş veya yanlış. Cloud Run service → env vars kontrol.
2. **Service account izni yok**: 
   ```
   gcloud projects add-iam-policy-binding <PROJECT_ID> \
     --member="serviceAccount:<PROJECT_ID>-compute@developer.gserviceaccount.com" \
     --role="roles/storage.objectAdmin"
   ```

Loglarda `data.gcs.upload_failed` arayabilirsin.

### Loglar nerede?

Üç yol:
1. **Telegram'da hata mesajındaki 📋 link** — direkt o hatanın log'una gider
2. **Cloud Run Console** → kitabi service → "LOGS" sekmesi
3. **Cloud Logs Explorer** → sorgu: `resource.type="cloud_run_revision" resource.labels.service_name="kitabi"`

Detay: README.md'nin "Logları nerede görüyorum?" bölümü.

### Botun adını / fotoğrafını değiştirmek

@BotFather'a git:
- `/setname` → bot seç → yeni ad gir
- `/setdescription` → bot seç → tanıtım metni
- `/setuserpic` → bot seç → foto yükle
- `/setabouttext` → "Hakkında" metni

### Botu silmek istiyorum

İki yer:
1. Telegram'da: @BotFather → `/deletebot` → bot seç → onayla (geri alınamaz, username 24 saat rezerve olur)
2. Google Cloud'da: Cloud Run service'i sil + GCS bucket'i sil + secret'ları sil

### Yeni özellik istiyorum

GitHub Issues'da öneri olarak aç: [github.com/poeple-app/kitabi/issues](https://github.com/poeple-app/kitabi/issues)

Kendin eklemek istiyorsan PR aç, README'deki "Katkı" bölümünü oku.

### Daha fazla yardım

📧 **poeple.api@gmail.com** — buradaki rehberlerle çözülmediyse.

Yazarken şunları ekle:
- Hangi adımda takıldın
- Aldığın hata mesajı (Telegram'da gelen veya Cloud Run logundan)
- Hangi adımları zaten denedin
