# Kitabi

> Telegram'da yaşayan, kişisel okuma günlüğü botun.

Kitabi sesli notlar, sayfa fotoğrafları ve sorularla okuma sürecini yakalar; Google Gemini ile kategorize eder, anlam katar, kitap bitiminde tasarımlı bir PDF okuma günlüğü üretir.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![Deploy](https://img.shields.io/badge/Deploy-Cloud%20Run-4285F4.svg)](https://cloud.google.com/run)

## Hızlı kurulum

**Tek tıklamayla kurulum sihirbazı**: [poeple-app.github.io/kitabi](https://poeple-app.github.io/kitabi) (GitHub Pages'te hosted)

Sihirbaz seni 7 adımda elinden tutarak götürür:
1. Telegram bot oluşturma
2. Gemini API anahtarı alma
3. GitHub fork
4. Google Cloud Project açma
5. Secret Manager'a anahtarları koyma
6. Cloud Run'a tek-tık deploy
7. Webhook bağlama

Toplam süre: ~30 dakika. Toplam maliyet: **0 TL** (tüm servislerin ücretsiz katmanları yeter).

## Ne yapar?

| Özellik | Detay |
|---|---|
| 🎤 **Sesli not** | Telegram'a ses gönder → Gemini transkript eder → kategori önerir → kaydeder |
| 📷 **Sayfa fotoğrafı** | Foto gönder → Gemini OCR → metin çıkarır → not olarak ekler |
| 🏷️ **Otomatik kategorize** | Alıntı / Fikir / Yeni Bilgi / Kelime / Kavram / Özet — Gemini önerir, sen onaylarsın |
| 📚 **Kelime + Kavram tanımı** | Bu kategorilerde Gemini otomatik tanım ekler |
| 💡 **"Açıkla" özelliği** | Bir notu Gemini ile genişlet, eleştir, bağlam ekle |
| ❓ **Soru-cevap modu** | "Bu kavram neydi?" gibi sor → Gemini kitap + notların context'iyle cevaplar |
| 📋 **Oturum recap'i** | Yeni oturum başında geçen özetlerini geri okur |
| 📕 **PDF günlüğü** | Kitap bitince güzel, tasarımlı PDF üretilir (kapak, künye, oturum kronolojisi, sözlük, istatistik) |
| 📤 **Esnek export** | PDF, JSON, CSV, Markdown, ZIP — tüm veriler senin Cloud Storage'ında, hep dışa alınabilir |

## Mimari

**Tek bir Google Cloud Project içinde 3 servis:**

```
   Telegram ──────► Cloud Run (kitabi/main.py)
                        │
                        ├─► Cloud Storage  (SQLite veritabanı)
                        │
                        ├─► Secret Manager (token + key)
                        │
                        └─► Gemini API     (AI)
```

**Tek bir SQLite veritabanı** → kişisel ölçekte fazlasıyla yeter, taşıma kolay, tüm veriler tek dosyada.

**Kod yapısı (4 Python dosyası):**

```
kitabi/
├── main.py    — FastAPI uygulama, webhook, lifecycle
├── bot.py     — Tüm Telegram UI (handlers, screens, dispatch)
├── data.py    — SQLAlchemy modelleri + tüm export'lar (PDF, JSON, CSV, MD, ZIP)
└── ai.py      — Gemini wrapper (retry + 3-model fallback)
```

## Gemini güvenilirliği

Gemini'nin model isimleri ve rate limit'leri zaman zaman değişiyor. Kitabi bunu otomatik tolere ediyor:

- **Model fallback zinciri**: `gemini-2.5-flash` → `gemini-2.0-flash` → `gemini-1.5-flash`
- **Per-model retry**: rate limit / 5xx / timeout durumunda 2 deneme exponential backoff ile
- **Graceful degradation**: kategori önerisi başarısız olursa "Yeni Bilgi" varsayılan; tanım/açıklama başarısız olursa not yine kaydedilir
- **Tüm hatalar kullanıcıya gösterilir**: kod bilmeyen kullanıcı bile hangi adımda nerede patladığını görür

## Logları nerede görüyorum?

Bot Google Cloud Run'da çalışıyor, log'lar **Cloud Logs Explorer**'a akıyor. Üç yolla erişebilirsin:

**1. Telegram hata mesajındaki link (en kolay)**

Bot bir şeye takılınca sana Telegram'da hata mesajı atar:

```
❌ Beklenmedik bir hata oluştu.

İşlem: handle_voice
Hata: GeminiCallFailed
Detay: ...

📋 [Bu hatanın detay loglarını aç] ← buraya tıkla
```

Tıkladığında doğrudan **o hatanın filtrelenmiş loglarına** gidersin — başka bir şey aramaya gerek yok.

**2. Cloud Run Console (servisin ana sayfası)**

[console.cloud.google.com/run](https://console.cloud.google.com/run) → `kitabi` servisini seç → üstte **"LOGS"** sekmesi. Son tüm loglar burada akıyor; severity (ERROR / WARNING / INFO) filtresi solda.

**3. Cloud Logs Explorer (gelişmiş arama)**

[console.cloud.google.com/logs](https://console.cloud.google.com/logs) → sorgu kutusuna:

```
resource.type="cloud_run_revision"
resource.labels.service_name="kitabi"
jsonPayload.event="ai.transcribe_voice.failed"
```

Bot içindeki her event'in stabil bir adı var (örnek: `bot.handler.handle_voice.failed`, `ai.transcribe_voice.success`, `data.add_note.start`). Telegram'daki hata mesajı sana hangi event'i aratacağını söylüyor.

### Logların yapısı

`structlog` ile JSON çıktı; her satır şunları içerir:
- `timestamp` (ISO 8601)
- `level` (info / warning / error)
- `module`, `func_name`, `lineno` (kodun tam yeri)
- `event` (olay etiketi)
- Ek context (süre, boyut, kullanıcı ID, hata tipi)
- Hata varsa `exc_info` ile tam stack trace

## Sorun giderme

| Belirti | Nereye bak |
|---|---|
| Bot Telegram'da cevap vermiyor | Cloud Run Logs → severity=ERROR, son 1 saat |
| "Beklenmedik hata" mesajı geldi | Mesajdaki 📋 link'e tıkla, doğrudan logu görürsün |
| Webhook çalışmıyor | Cloud Run service URL'ine GET at — 200 dönmeli; `main.webhook.bad_secret` log'u varsa secret yanlış |
| Gemini cevap vermiyor | `ai._call_gemini.exhausted` event'ini ara — tüm modeller başarısız olmuş demektir; quota / API key sorun |
| Veri kayboldu | `data.gcs.upload_failed` veya `data.gcs.init_failed` aratabilirsin; service account'a Storage Object Admin yetkisi verildi mi kontrol et |
| PDF üretilmiyor | `data.render_pdf.failed` aratabilirsin; WeasyPrint dependency'leri Docker image'da yüklü mü? |

## Güvenlik

- ✅ Tüm secret'lar **Google Cloud Secret Manager**'da, yerel dosyada asla değil
- ✅ Telegram webhook secret_token doğrulaması (sahte istekleri reddet)
- ✅ Kullanıcı allowlist (`ALLOWED_TG_USER_IDS` — sadece sen yazabilirsin)
- ✅ `pre-commit` + `detect-secrets` + `gitleaks` — yanlışlıkla secret commit'ini engeller
- ✅ Container non-root olarak çalışır
- ✅ Log redaction — kullanıcı içeriği (notlar, sorular) log'a düşmez, sadece event'ler

## Geliştirme

```bash
# Repo'yu fork'la, klonla
git clone https://github.com/<senin-kullanici-adin>/kitabi.git
cd kitabi

# Python 3.11+ sanal ortam
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Bağımlılıkları kur
pip install -e ".[dev]"

# pre-commit hook'ları aktif et
pre-commit install

# Lokal çalıştırma (.env dosyası gerek)
uvicorn kitabi.main:app --reload --port 8080
```

`.env` dosyasını `.env.example`'dan kopyala ve doldur (lokal dev için; prod'da Secret Manager kullanılır).

## Mimari kararlar (FAQ)

**Neden Notion değil?**
Kullanıcıyı belirli bir araca bağlamak istemiyoruz. Veri SQLite'da yaşıyor, istediğin gibi export ediyorsun. Notion isteyenler için ileride opt-in connector eklenebilir.

**Neden Python?**
WeasyPrint (PDF) Python'da en olgun, Gemini SDK'sı Python-first, async ekosistem stabil.

**Neden Cloud Run + Cloud Storage + SQLite?**
- Cloud Run: scale-to-zero, public HTTPS, container deploy
- Cloud Storage: 5 GB free, SQLite dosyası buraya mount ediliyor → kalıcılık
- SQLite: tek dosya, basit, kişisel ölçekte yeter

**Tek instance limiti neden?**
SQLite concurrent write'ı paralel container'lardan iyi yönetmiyor. Cloud Run `max-instances=1` ile garanti veriyoruz. Yüksek trafik istersek Turso (managed libSQL) veya Postgres'e geçmek tek dosya değiştirmek.

**Birden fazla kullanıcı destekleniyor mu?**
Bu sürüm **single-user-per-deployment**. Sen kendi botunu host ediyorsun, sadece sana cevap veriyor. Multi-tenant (başka kullanıcılara da hizmet) ayrı bir proje olur.

## Katkı

Pull request'ler hoş karşılanır.

- Yeni bir özellik için önce issue aç, tartışalım
- Branch'i `feature/açıklama` formatında isimlendir
- `pre-commit run --all-files` ve `ruff check` temiz olmalı
- Kullanıcıya görünür her metin Türkçe; kod ve yorumlar İngilizce

## Lisans

[MIT](LICENSE) — özgürce kullan, değiştir, dağıt.

## Açıklama (Disclaimer)

Kitabi **kişisel bir açık kaynak projesidir** ve "olduğu gibi" sunulur. Hiçbir garanti verilmez; kullanım tamamen kendi sorumluluğundadır. Kitabi geliştiricileri kullanıcının hiçbir kişisel verisini, kitap notunu, sohbet içeriğini veya kimlik bilgisini toplamaz — tüm veriler yalnızca kullanıcının kendi Google Cloud hesabında kalır. Servislerin (Telegram, Google, GitHub) kullanım koşulları kullanıcıyı bağlar.
