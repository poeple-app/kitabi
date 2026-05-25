# Kitabi

> Telegram'da yaşayan, kişisel okuma günlüğü botun.

Kitabi sesli notlar, sayfa fotoğrafları ve sorularla okuma sürecini yakalar; Google Gemini ile kategorize eder, anlam katar, kitap bitiminde tasarımlı bir PDF okuma günlüğü üretir.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![Deploy](https://img.shields.io/badge/Deploy-Cloud%20Run-4285F4.svg)](https://cloud.google.com/run)

## Hızlı kurulum

**Tek tıklamayla kurulum sihirbazı**: [poeple-app.github.io/kitabi/wizard.html](https://poeple-app.github.io/kitabi/wizard.html)

Sihirbaz seni **9 adımda** elinden tutarak götürür:

1. Telegram bot oluşturma
2. Gemini API anahtarı alma
3. GitHub fork (ya da ZIP yolu — fork gerek yok)
4. Google Cloud Project + 4 API enable
5. **Cloud Storage bucket** (SQLite kalıcılığı için)
6. Secret Manager'a 4 anahtarı koyma
7. Cloud Run'a tek-tık deploy
8. `BOT_BASE_URL` set + redeploy → webhook
9. (opsiyonel) Cloud Scheduler → proaktif hatırlatma

Toplam süre: **~40-55 dakika**. Toplam maliyet: **0 TL** (tüm servislerin ücretsiz katmanları yeter).

**Yazılım biliyorum, sihirbazı atlamak istiyorum** → wizard'ın welcome ekranındaki "🛠️ Manuel kurulum" link'ine bas, tek sayfada `gcloud` komutlarıyla bitir (~15-25 dk).

## Ne yapar?

| Özellik | Detay |
|---|---|
| 🎤 **Sesli not** | Telegram'a ses gönder → Gemini transkript eder → kategori önerir → kaydeder |
| 📷 **Vurgu odaklı sayfa OCR** | Foto gönder → bot SADECE altı çizili / fosforlu / kalemle vurgulanmış metni çıkarır, tam sayfayı kopyalamaz |
| 💬 **Foto + caption = soru** | Fotoğrafa caption (açıklama) eklersen → Gemini sayfayı okur, talimatını yapar. Karmaşık komutlar da OK: "şu cümleyi al + 'idealist'in sözlük anlamını ekle". Çıktı OCR + Tanım/Cevap/Özet bloğu |
| 📸 **Kapak fotoğrafıyla kitap ekleme** | Kitap kapağını çek → Gemini ISBN/başlık/yazar tanır → metadata + kapak otomatik gelir |
| 🌐 **ISBN çift kaynak** | Google Books başarısız olursa Open Library'ye otomatik fallback; 429 / boş sonuç olsa bile kitap bulunur. Tire/boşluk içeren ISBN'ler otomatik temizlenir |
| 🖼️ **Orphan photo (sahne)** | Vurgu/OCR yoksa bot kitapla ilgisi olmayan görsel sayar; senden bir not alır, PDF'te "📷 Sahne" olarak gösterir |
| 🏷️ **Otomatik kategorize** | Alıntı / Fikir / Yeni Bilgi / Kelime / Kavram / Özet — Gemini önerir, sen onaylarsın |
| ➕ **Custom kategoriler** | Kendi etiketlerini sınırsız ekle ("Refleksiyon", "Tartışma" vb.). Notlarım hub'ında sayım belirteciyle görünür |
| 📚 **Kelime + Kavram tanımı** | Bu kategorilerde Gemini otomatik tanım ekler |
| 💡 **"Açıkla" özelliği** | Bir notu Gemini ile genişlet, eleştir, bağlam ekle |
| ❓ **Soru-cevap modu** | "Bu kavram neydi?" gibi sor → Gemini kitap + notların context'iyle cevaplar |
| ✂️ **Öz Gemini cevapları** | Tüm AI çıktıları "dolgu kelime yok, ≤3 cümle" kuralıyla üretilir — bilgi kaybı yok, gürültü yok |
| 📑 **Çoklu oturum** | Aynı anda birden fazla kitap okuyabilirsin; bot doğru oturuma yönlendirir |
| ✏️ **Oturum düzenle/sil** | Aktif oturumda sayfa numarasını düzelt veya yanlış açılan oturumu (+notları) sil; açık oturum listesinden direkt erişim |
| 🔤 **Kısa kod sistemi** | Her kitap/oturum/not human-readable kod alır: `SVC`, `SVC-S03`, `SVC001` |
| 📋 **Oturum recap'i** | Yeni oturum başında geçen özetlerini geri okur |
| 📝 **Notlarım hub** | Ana menüde tüm not kategorilerini sayımlarıyla topluca gör: Alıntı (7), Fikir (2), Kavram (3)… Custom kategorileri ve "➕ Yeni kategori ekle" de burada |
| 🔍 **Tam-metin arama** | Tüm notlarında FTS5 ile anında arama; ilk 5 sonuç, "Daha fazla göster" ile +5'er |
| 📖 **Sözlük + tag cloud** | Tüm Kelime + Kavram notları alfabetik; PDF'te bir de kelime bulutu var |
| 💬 **Alıntılar + favoriler** | Tüm Alıntı notların tek yerde; ⭐ ile favori işaretle |
| 📤 **Not paylaş (Klasik Twit)** | Bir alıntıyı/notu pull-quote kart olarak paylaş — Crimson Pro / Playfair / Cormorant / EB Garamond / Lora / Merriweather font seçenekleri, 5 boyut (Kare, IG Post, IG Story, A4, A5). Uzun metinde font otomatik küçülür, boşluklar sabit. GitHub linki footer'da |
| 📜 **"…devamını oku"** | 500 karakteri/10 satırı geçen notlar listede ve detayda kısa görünür; bir tıkla tam metin açılır |
| 📊 **Detaylı istatistik** | Streak, en verimli zaman aralığı, oturum dağılımı (ana menüde de özet) |
| 🎨 **Kitap ikonu + raflar** | Her kitap kendi emoji ikonunu alır. 10+ kitapta otomatik raf sistemi ("Felsefe", "Tarih"…) devreye girer |
| 🛠️ **Kullanıcı tanımlı alanlar** | Kitap düzenleme menüsünden "Raf kodu", "Ödünç verildi" gibi kendi sütunlarını ekle; sınırsız |
| 📸 **Kapak grid (albüm)** | "Kapakları topluca göster" → kütüphane kapakları Telegram albümü olarak (10'arlı) gönderilir |
| 🏁 **Bitirme ritüeli** | Kitap bitince ⭐ puan + tek cümlelik yorum + favori alıntı seçimi + öneri |
| 📕 **Zengin PDF günlüğü** | Kapak (rating + review), künye (yayınevi + yazarın diğer 3 kitabı + özel alanlar), istatistik (okuma takvimi + sayfa/kelime/kavram sayaçları), oturum kronolojisi (notlara eklenmiş fotoğraflar gömülü olarak), kelime bulutu, sözlük, favori alıntılar, yeniden tasarlanmış kapanış |
| 🗂️ **Esnek export** | PDF, JSON, CSV, Markdown, ZIP — tüm veriler senin Cloud Storage'ında, hep dışa alınabilir |
| 🔔 **Proaktif hatırlatma** | Bot "uzun süredir okumadın", "hâlâ okuyor musun?" gibi nudge'lar atar (opsiyonel) |
| ⚙️ **Ayarlar** | Hatırlatma sıklığı, otomatik kategori, otomatik açıklama — hepsi tek menüde toggle |
| 🔄 **Şeffaf işlem mesajları** | Uzun süren AI çağrılarında (ASR, OCR, soru-cevap, PDF, export) "🔄 İşleniyor…" placeholder mesajı, bitince silinir |
| ⚡ **Hızlı butonlar** | Chat input kutusunun altında her zaman görünen 4 kalıcı buton: 🟢 Oturumlar / ⏹️ Bitir / 📖 Kitaplar / ➕ Yeni |
| 🪟 **Tek aktif menü** | Önceki menü mesajı her yeni ekrandan önce otomatik silinir — sohbet kalabalıklaşmıyor |

## Slash komutları

Telegram'ın `/` menüsünde her özelliğe kestirme komut var:

- `/start` — Ana menü
- `/oturum` — Yeni okuma oturumu başlat
- `/oturumlar` — Açık oturumları gör, düzenle ya da sil
- `/kitaplar` — Kütüphanedeki kitaplar (10+ kitapta raflara döner)
- `/yeni` — Yeni kitap ekle (yazı / ISBN / kapak fotoğrafı)
- `/ara` — Notlarda ara
- `/sozluk` — Sözlük (Kelime + Kavram)
- `/alintilar` — Alıntılar
- `/istatistik` — İstatistik
- `/ayarlar` — Bot ayarları
- `/yardim` — Kısa kullanım rehberi

## Sürüm geçmişi — neler değişti

### v1.0.4 (en son)
- 🪲 **Çift ikon fix**: kitap listesinde "📖 📖" yerine tek 📖
- 🔢 **Notlarım sayım belirteçleri**: Fikir (2), Yeni Bilgi (5), Kavram (3)…
- ➕ **Custom not kategorileri**: kullanıcı kendi etiketlerini ekler (Refleksiyon, Tartışma…). Note.category_label + AppSettings.custom_categories
- 💬 **Foto + caption iyileştirme**: caption serbest talimat — OCR + Tanım/Cevap/Özet etiketleriyle yapılandırılmış çıktı
- 🟢 **Post-save action butonları**: foto+caption sonrası "Aktif Oturuma Dön / Bu Notu Aç / Yeni Foto / Ana Menü"
- 🖼️ **PDF'te foto embed**: notlara eklenmiş fotoğraflar artık PDF günlüğünde gerçekten görünür. render_pdf öncesi Telegram API'den indirilip base64 data URI olarak HTML'e gömülür

### v1.0.3
- 💬 **Foto + caption = Q&A**: fotoğrafa caption yazarsan sayfayı okuyup talimatını uygular
- 📷 **Vurgu odaklı OCR**: caption yoksa sadece altı çizili/vurgulu metni alır, tam sayfayı kopyalamaz
- 📝 **Notlarım hub**: ana menüye yeni "📝 Notlarım" çatısı (Alıntı/Fikir/Yeni Bilgi/Kavram/Kelime/Özet tek menüde)
- 🪟 **Tek aktif menü**: yeni ekran açılırken eski bot menüsü otomatik silinir
- 📜 **"…devamını oku"**: 10 satırdan / 500 karakterden uzun notlar listede kısa görünür, tıklayınca açılır
- ⚡ **Hızlı butonlar** (ReplyKeyboard): 🟢 Oturumlar / ⏹️ Bitir / 📖 Kitaplar / ➕ Yeni — chat kutusu altında kalıcı
- 📤 **"Klasik Twit" not paylaşımı**: pull-quote tasarımı, 6 Google Font seçeneği (Crimson Pro, Playfair, Cormorant, EB Garamond, Lora, Merriweather), uzun metinde font otomatik küçülür

### v1.0.2
- 🌐 **ISBN çift kaynak**: Google Books başarısız olursa Open Library devreye girer (anonim Cloud Run IP'leriyle 429 sorunu çözüldü)
- 📷 **Orphan photo akışı**: OCR'siz görseli "sahne" notu olarak sakla, PDF'te ayrı stillendirilmiş bölüm
- 📕 **Yeniden tasarlanmış PDF**: okuma takvimi (ay grid), kelime bulutu, yazarın diğer 3 kitabı, özel alanlar, yeniden organize edilmiş kapanış sayfası
- 🛠️ **Zenginleştirilmiş kitap düzenleme**: title/yazar/yayınevi/yıl/genre/ISBN dahil tüm alanlar düzenlenebilir; kullanıcı kendi alanını ekleyebilir (Book.extra_fields JSON)
- 🎨 **Kitap ikonu + raf sistemi**: her kitap kendi emoji'sini alır; 10+ kitapta otomatik raf landing sayfası
- 📸 **Kapak grid albümü**: kapakları Telegram media group olarak topluca gönder
- 📤 **Not paylaş**: bir notu twit görseli gibi 5 farklı boyutta PDF olarak çıkar
- ✂️ **Daha öz Gemini cevapları**: 12 dolgu kelimesi açıkça yasaklı, ≤3 cümle kuralı
- ⬇️ **"Daha fazla göster" pagination**: alıntı/sözlük/arama listelerinde
- 🔄 **Progress mesajları her uzun işlemde**: PDF, export, AI çağrıları
- 📋 **`/oturumlar` ve `/yardim` slash komutları**
- 🗂️ **Otomatik DB migration**: v1.0.1 → v1.0.2 geçişi tek redeploy ile (yeni sütunlar ALTER TABLE ile eklenir)

## Mimari

**Tek bir Google Cloud Project içinde 4 servis:**

```
   Telegram ──────► Cloud Run (kitabi/main.py)
                        │
                        ├─► Cloud Storage   (SQLite veritabanı snapshot)
                        ├─► Secret Manager  (token + key)
                        ├─► Gemini API      (AI: ASR, OCR, kategori, soru-cevap, yazar bilgisi)
                        ├─► Google Books    (ISBN/title metadata — birinci kaynak)
                        ├─► Open Library    (ikinci kaynak; Google Books 429 / boş olunca)
                        │
   Cloud Scheduler ─────┘   (günlük "hâlâ okuyor musun?" / "kitap bekliyor")
```

**Persistence:** Cloud Run diski uçucu — bot SQLite'ı her 60 sn'de bir Cloud Storage bucket'a SQLite Online Backup API ile tutarlı snapshot olarak yedekliyor; container açılırken indiriyor.

**Şema migration:** Her container başlangıcında `_migrate_add_missing_columns` çalışır. ORM modelinde olup tablodaki olmayan sütunları `ALTER TABLE ADD COLUMN` ile ekler. v1.0.1 DB'si v1.0.2 koduyla otomatik uyumlu hale gelir — manuel migration script gerekmez.

**Kod yapısı (5 Python dosyası):**

```
kitabi/
├── main.py    — FastAPI app, webhook, lifecycle, /cron/nudge endpoint
├── bot.py     — Tüm Telegram UI (handlers, screens, dispatch dict)
├── data.py    — SQLAlchemy + GCS backup + FTS5 + tüm export'lar
├── ai.py      — Gemini wrapper (3-model fallback + retry)
└── dev.py     — Lokal polling modu (webhook olmadan test için)
```

## Gemini güvenilirliği

Gemini'nin model isimleri ve rate limit'leri zaman zaman değişiyor. Kitabi bunu otomatik tolere ediyor:

- **Model fallback zinciri**: `gemini-2.5-flash` → `gemini-2.0-flash` → `gemini-1.5-flash`
- **Per-model retry**: rate limit / 5xx / timeout durumunda 2 deneme exponential backoff ile
- **Graceful degradation**: kategori önerisi başarısız olursa "Yeni Bilgi" varsayılan; tanım/açıklama başarısız olursa not yine kaydedilir
- **Tüm hatalar kullanıcıya gösterilir**: kod bilmeyen kullanıcı bile hangi adımda nerede patladığını görür ve doğrudan Cloud Logs deep-link'i alır

## Logları nerede görüyorum?

Bot Google Cloud Run'da çalışıyor, log'lar **Cloud Logs Explorer**'a akıyor. Üç yol var:

**1. Telegram hata mesajındaki link (en kolay)** — bot hata atınca mesajdaki 📋 link doğrudan o hatanın filtrelenmiş log'una götürür.

**2. Cloud Run Console** — [console.cloud.google.com/run](https://console.cloud.google.com/run) → `kitabi` → **LOGS** sekmesi.

**3. Cloud Logs Explorer** (gelişmiş arama):

```
resource.type="cloud_run_revision"
resource.labels.service_name="kitabi"
jsonPayload.event="ai.transcribe_voice.failed"
```

Bot içindeki her event'in stabil bir adı var (`bot.handler.handle_voice.failed`, `data.add_note.success`, `bot.nudge.still_reading_sent`, vb.). Telegram hata mesajı sana hangi event'i aratacağını söyler.

### Logların yapısı

`structlog` ile JSON çıktı; her satır şunları içerir:
- `timestamp` (ISO 8601), `level` (info / warning / error)
- `module`, `func_name`, `lineno` (kodun tam yeri)
- `event` (olay etiketi) + ek context (süre, boyut, kullanıcı ID, hata tipi)
- Hata varsa `exc_info` ile tam stack trace

Detaylı sorun-giderme rehberi için [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Güvenlik

- ✅ Tüm secret'lar **Google Cloud Secret Manager**'da; yerel dosyada / repo'da asla değil
- ✅ Webhook `X-Telegram-Bot-Api-Secret-Token` header'ı `hmac.compare_digest` ile karşılaştırılır (sahte istekleri reddet)
- ✅ Kullanıcı allowlist (`ALLOWED_TG_USER_IDS`) — sadece sen yazabilirsin
- ✅ SQLite WAL mode + Online Backup API → torn-snapshot-safe yedekleme
- ✅ Chat state SQLite'ta persist edilir → cold-start'ta yarım kalmış akışlar kaybolmaz
- ✅ `pre-commit` + `detect-secrets` + `gitleaks` — yanlışlıkla secret commit'ini engeller (lokal dev için)
- ✅ Container non-root olarak çalışır
- ✅ Log redaction — kullanıcı içeriği (notlar, sorular) log'a düşmez, sadece event'ler

## Geliştirme

### Lokal test — gerçek Telegram, polling modu (Cloud Run gerek yok)

```powershell
git clone https://github.com/poeple-app/kitabi.git
cd kitabi
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

# Secret'ları env'e inject et (lokal dosyaya yazma! HARD RULE)
$env:TELEGRAM_BOT_TOKEN  = "<BotFather_token>"
$env:ALLOWED_TG_USER_IDS = "<senin_user_id>"
$env:GEMINI_API_KEY      = "<gemini_api_key>"

python -m kitabi.dev
```

Bu polling modu webhook + GCS + FastAPI olmadan çalışır; gerçek Telegram istemcide test edebilirsin. SQLite `./kitabi-dev.db` olarak yerelde tutulur.

### Testler

```powershell
pytest tests/
```

## Mimari kararlar (FAQ)

**Neden Notion değil?** Kullanıcıyı belirli bir araca bağlamak istemiyoruz. Veri SQLite'da yaşıyor, istediğin gibi export ediyorsun.

**Neden Python?** WeasyPrint (PDF) Python'da en olgun, Gemini SDK'sı Python-first, async ekosistem stabil.

**Neden Cloud Run + Cloud Storage + SQLite?**
- Cloud Run: scale-to-zero, public HTTPS, container deploy
- Cloud Storage: 5 GB free, SQLite snapshot'ı buraya yedekleniyor → kalıcılık
- SQLite: tek dosya, basit, kişisel ölçekte yeter

**Tek instance limiti neden?** SQLite concurrent write'ı paralel container'lardan iyi yönetmiyor. Cloud Run `max-instances=1` ile garanti veriyoruz.

**Birden fazla kullanıcı destekleniyor mu?** Hayır — bu sürüm **single-user-per-deployment**. Sen kendi botunu host ediyorsun, sadece sana cevap veriyor (`ALLOWED_TG_USER_IDS` allowlist). Multi-tenant ayrı bir proje olur.

## Katkı

Pull request'ler hoş karşılanır.

- Yeni bir özellik için önce issue aç
- Branch'i `feature/açıklama` formatında isimlendir
- `pre-commit run --all-files` ve `ruff check` temiz olmalı
- Kullanıcıya görünür her metin Türkçe; kod ve yorumlar İngilizce

## Lisans

[MIT](LICENSE) — özgürce kullan, değiştir, dağıt.

## Açıklama (Disclaimer)

Kitabi **kişisel bir açık kaynak projesidir** ve "olduğu gibi" sunulur. Hiçbir garanti verilmez; kullanım tamamen kendi sorumluluğundadır. Kitabi geliştiricileri kullanıcının hiçbir kişisel verisini, kitap notunu, sohbet içeriğini veya kimlik bilgisini toplamaz — tüm veriler yalnızca kullanıcının kendi Google Cloud hesabında kalır. Servislerin (Telegram, Google, GitHub) kullanım koşulları kullanıcıyı bağlar.
