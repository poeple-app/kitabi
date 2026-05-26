<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="kitabilogo-white.png">
    <img src="kitabilogo.png" alt="Kitabi" width="220" />
  </picture>
</p>

# Kitabi

> Telegram'da yaşayan, kişisel okuma günlüğü botun.

Kitabi sesli notlar, sayfa fotoğrafları ve sorularla okuma sürecini yakalar; Google Gemini ile kategorize eder, anlam katar, kitap bitiminde tasarımlı bir PDF okuma günlüğü üretir.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org)
[![Deploy](https://img.shields.io/badge/Deploy-Cloud%20Run-4285F4.svg)](https://cloud.google.com/run)

## Kurulum — 3 yol

### ⚡ Hızlı kurulum (10-15 dk) — ÖNERİLEN

GitHub hesabı gerekmez. Cloud Shell'de tek komut + 3 değer yapıştırma.

```bash
curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/install.sh | bash
```

**Adımlar:**
1. [shell.cloud.google.com](https://shell.cloud.google.com) aç (Google ile giriş yap)
2. Yukarıdaki komutu yapıştır, Enter
3. Script senden 3 şey isteyecek:
   - Telegram bot token (BotFather'dan, ~2 dk)
   - Telegram user ID (@userinfobot'tan, ~1 dk)
   - Gemini API key ([aistudio.google.com](https://aistudio.google.com/app/apikey), ~1 dk)
4. ~4 dakika Cloud Run build → bot çalışıyor

**Önkoşullar:** Gmail hesabı + Google Cloud için bir kart (çekim yok, free tier).

**Güncelleme zamanı geldiğinde:**
```bash
curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/update.sh | bash
```

### 📋 Görsel sihirbaz (~45 dk)

[poeple-app.github.io/kitabi/wizard.html](https://poeple-app.github.io/kitabi/wizard.html) — 9 adımı tarayıcıdan görsel olarak ilerlemek istersen. GitHub fork ile otomatik güncelleme dahil.

### 🛠️ Tek sayfa manuel (~15 dk)

Kurulum sayfasının karşılama ekranında "🛠️ Manuel kurulum" — `gcloud` komutlarıyla tek sayfa referans dokümantasyonu.

---

**Toplam maliyet (her 3 yol için):** 0 TL — tüm servislerin ücretsiz katmanlarında kalıyorsun.

## Ne yapar?

| Özellik | Detay |
|---|---|
| 🎤 **Sesli not** | Telegram'a ses gönder → Gemini transkript eder → kategori önerir → kaydeder |
| 📷 **Vurgu odaklı sayfa OCR** | Foto gönder → bot SADECE altı çizili / fosforlu / kalemle vurgulanmış metni çıkarır, tam sayfayı kopyalamaz. v1.0.5: temperature=0 + chain-of-thought + renk tespiti ile sınır doğruluğu yüksek |
| 💬 **Foto + caption = soru** | Fotoğrafa caption (açıklama) eklersen → Gemini sayfayı okur, talimatını yapar. Karmaşık komutlar da OK: "şu cümleyi al + 'idealist'in sözlük anlamını ekle". Çıktı tırnaklı italic OCR + TANIM/CEVAP/ÖZET etiketleri |
| ✂️ **Tire fix** | OCR'da "tahak-\nküm" → "tahakküm" otomatik; satır sonu kelime kırılmaları temizlenir |
| 🖼️ **Telegram'da not foto** | Not detayı açıldığında, eklenmiş fotoğraf da caption ile birlikte gösterilir (kitap kapağı pattern'iyle aynı) |
| 📎 **Kaynak dipnotu** | Gemini cevaplarında otomatik "Kaynak: KOD1, KOD2" footer'ı — soruyu cevaplarken hangi notlardan yararlanıldığı şeffaf |
| ⏳ **Anında buton feedback** | Her tıklama sonrası 50ms içinde toast popup; uzun callback'lerde de kullanıcı "tıklandı" bilgisi alır, ekstra API call yok |
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

## Örnek çıktılar (Yeraltından Notlar — Dostoyevski)

Botun ne ürettiğini somut görmek için örnek bir okumadan derlenmiş kısa bir kesit. Aşağıdaki içerikler bir gerçek okuma oturumundan değil, **göstermelik mockup** — kendi okumanda yapı aynı olur, doldurma sen olursun.

<table>
  <tr>
    <td align="center" width="180">
      <a href="mockups/yeralt-notlar.html">
        <img src="https://upload.wikimedia.org/wikipedia/commons/thumb/8/87/PDF_file_icon.svg/120px-PDF_file_icon.svg.png" width="64" alt="PDF" /><br/>
        <strong>📕 Örnek günlük</strong><br/>
        <sub>Tarayıcıda aç → tüm sayfaları gör → istersen Ctrl+P ile PDF olarak indir</sub>
      </a>
    </td>
    <td>
      <b>Yeraltından Notlar</b> — Dostoyevski<br/>
      <i>İletişim Yayınları, 2017 · 188 sayfa · 6 oturum · 23 not</i><br/><br/>
      Bot bittiğinde böyle bir PDF üretiyor: <b>kapak</b> (rating + tek-cümlelik review) → <b>künye</b> (yayınevi + yazarın diğer 3 kitabı + senin özel alanların) → <b>istatistik</b> (okuma takvimi + kategori dağılımı + kelime/kavram sayısı) → <b>oturum kronolojisi</b> (alıntılar / fikirler / kavramlar zaman sırasıyla) → <b>favori alıntılar</b> → <b>kelime + kavram sözlüğü</b> (Notion-style etiket bulutu + tanımlar) → <b>kapanış sayfası</b>.
    </td>
  </tr>
</table>

### 📕 Kitap detay ekranı (Telegram)

```
🏠 Ana › 📚 Kitaplarım › Yeraltından Notlar

📖 Yeraltından Notlar  ·  YAN
✍️ Fyodor Dostoyevski
🏢 İletişim Yayınları · 2017
🏷️ Roman › Felsefi Roman
🌐 ISBN: 9789750507380
📚 Raf: 📚 Felsefi roman

📊 142 / 188 sayfa (%75)
📅 6 oturum
📝 23 not
🔖 Durum: Okuyor

🛒 İdefix · 67 TL
   12 Mart 2026

🏷️ #klasik #19yy #rus #varoluş

[ Kişisel alanlar: ]
  • Önerildi mi: Bir arkadaş (Defne)
  • İlk okuma: Hayır
```

### 💬 Birkaç örnek not

| Kod | Kategori | Sayfa | İçerik |
|---|---|---|---|
| `YAN003` | Alıntı | s.12 | *"Ben hasta bir adamım… Kötü bir adamım. Sevimsiz bir adamım."* |
| `YAN007` | Kavram | s.34 | **Bilinç** — Yeraltı adamı için aşırı bilinç hastalıktır; çünkü eylem yapacak yerde sonsuza dek kendini izler. |
| `YAN011` | Fikir | s.58 | Anlatıcının "iki kere ikinin dört etmesi" karşı çıkışı modern aklın sınırlarına dair en güçlü itirazlardan biri. |
| `YAN014` | Yeni Bilgi | s.81 | 1864'te yazılan kitap, varoluşçuluğun "habercisi" sayılıyor — Sartre ve Camus'tan yarım yüzyıl önce. |
| `YAN018` | 📷 Sahne | s.110 | *(fotoğraf eklenmiş)* "Kahvaltıda kahvemin yanında okurken çarpılarak işaretlediğim sayfa." |
| `YAN021` | 🏷️ Refleksiyon | s.134 | (Kullanıcı tanımlı kategori) Bu adamın özgürlük diye sunduğu şey aslında kendi içine hapsolma. Belki bir tür Stockholm. |

### 📤 Paylaşılabilir alıntı kartı (Klasik Twit — Crimson Pro)

```
┌──────────────────────────────────────────────┐
│  K I T A B I              okuma günlüğü      │
│                                              │
│  "                                           │
│                                              │
│  İki kere iki dört eder, gene de iki         │
│  kere iki beş eder bazen, hoş bir            │
│  şeycik olabilir.                            │
│                                              │
│                                              │
│  Yeraltından Notlar                          │
│  Fyodor Dostoyevski                          │
│  ──────────────────────────────────          │
│  s.62 · YAN012          Faruk · 25.05.2026   │
│                                              │
│           github.com/poeple-app/kitabi       │
└──────────────────────────────────────────────┘
```

### 📕 PDF okuma günlüğü — içindekiler özeti

Kitap bittiğinde üretilen PDF'in iskeleti:

1. **Kapak sayfası** — Yeraltından Notlar / Fyodor Dostoyevski / ⭐⭐⭐⭐⭐ / *"Modernliğin temellerini sarsan kısa bir başyapıt."* / 12 Mart - 28 Mayıs 2026
2. **Künye** — yayınevi, ISBN, sayfa sayısı, satın alma bilgisi, etiketler, **Dostoyevski'nin diğer önemli eserleri** *(Suç ve Ceza, Karamazov Kardeşler, Budala — Gemini'den)*, kişisel alanlar
3. **Okuma istatistikleri** —
   - 6 oturum · 11 saat 23 dakika · 188 sayfa
   - **Okuma takvimi** (Mart-Mayıs ay grid'i, yoğun günler koyu)
   - Kategori dağılımı: 8 Alıntı, 5 Fikir, 4 Yeni Bilgi, 3 Kavram, 2 Kelime, 1 Özet
4. **Okuma günlüğü** — 6 oturum kronolojik sırada, her oturumda alınan notlar (fotoğraflar sola float'lanmış olarak)
5. **Favori Alıntılar** — bitirme ritüelinde seçtiğin 3 favori
6. **Kelime ve Kavram Sözlüğü** — Notion-style etiket bulutu (Bilinç, Yeraltı, İrade, Aşırı bilinç, Determinizm…) + alfabetik liste + tanımlar
7. **Kapanış** — bir bakışta tüm sayılar, oturum özetleri (kronolojik), tarih + imza

### Örnek bir "foto + caption = soru" akışı

Kullanıcı bir sayfayı çekti, yeşil fosforla **"İki kere iki dört eder"** cümlesinin altını çizdi. Caption olarak yazdı:
> *"alıntıyı al + bu cümlenin felsefi anlamını da ekle"*

Bot şunu üretti:

```
📷 Fotoğraf + talimat  ·  YAN  Yeraltından Notlar

❓ alıntıyı al + bu cümlenin felsefi anlamını da ekle

🤖 "İki kere iki dört eder, gene de iki kere iki beş eder
   bazen, hoş bir şeycik olabilir."

   AÇIKLAMA: Bu cümle, anlatıcının aklın mutlak otoritesine
   karşı çıkışıdır. "İki kere iki" rasyonel sertlik, "beş"
   ise iradenin keyfi seçimi — özgürlük matematik dışıdır.

   Not olarak kaydedildi.
```

---

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

## Cold start'tan kaçınma (opsiyonel — botu hep sıcak tut)

Cloud Run **scale-to-zero**: 15 dakika kullanılmazsa container'ı kapatıyor. Sonraki ilk tıklamada container ~10-15 saniyede yeniden başlıyor — kullanıcı tarafında "tıkladım, hiçbir şey olmadı" hissi.

**Çözüm:** Cloud Scheduler 10 dakikada bir `/healthz` endpoint'ini çağırırsa container hep sıcak kalır.

Cloud Shell'de bir kerelik komut:

```bash
gcloud scheduler jobs create http kitabi-keep-warm \
  --location=europe-west1 \
  --schedule="*/10 * * * *" \
  --uri="$(gcloud run services describe kitabi --region=europe-west1 --format='value(status.url)')/healthz" \
  --http-method=GET
```

**Maliyet:** 8640 istek/ay × ~1 sn vCPU = 144 dakika. Cloud Run free tier'ı 180,000 sn (~50 saat) → sınırın **çok altında**.

**Etki:** İlk tıklama gecikmesi 10-15 sn → 100-400 ms.

İstemiyorsan atla; bot yine çalışır, sadece uzun aradan sonraki ilk tıklama yavaş olur.

## Sürüm geçmişi — neler değişti

### v1.0.5 (en son)
- 🎯 **OCR doğruluğu** — temperature=0 + chain-of-thought + renk tespiti. Vurgu sınırları artık katı; "bazen" gibi yakın kelimeler eklenmiyor
- ✂️ **Tire fix** — satır sonu "tahak-\\nküm" → "tahakküm". OCR ve foto+caption cevaplarında otomatik
- 🖼️ **Telegram not detayında foto** — note'a eklenmiş görsel, not açıldığında caption ile birlikte gösterilir (PDF'tekiyle aynı pattern)
- 📎 **Gemini cevaplarına kaynak dipnotu** — "Kaynak: SVC002, SVC007" formatında not referansları otomatik gelir
- ⏳ **Buton tıklama feedback** — her tıklamadan hemen sonra "⏳ Hazırlanıyor…" toast'u; ekstra API call yok, mevcut answer çağrısı öne çekildi
- 🪟 **Tek aktif menü sıkılaştırma** — user input (text/voice/photo) geldiğinde önceki menü otomatik silinir; callback edit dalında "yetim" başka menüler varsa onlar da temizlenir
- 📜 **"…devamını oku" yayılımı** — not taslağında (yeni not ekleme ekranında) uzun transkript / tanım / açıklama kısaltılır, "devamını oku" butonu tam metni gösterir
- 🏷️ **Kelime bulutu yeniden** — Notion-style multi-select tag chip'leri; tüm tag'ler aynı boyutta, ≤30 char/≤3 kelimeli olanlar cloud'da, cümleler alfabetik listede
- 🎨 **PDF foto layout** — notlara eklenmiş fotoğraflar artık SOLA float, yazı SAĞA akar — yer daha verimli kullanılır
- 📷 **Photo+caption format** — Gemini çıktısı yapılandırıldı: tırnaklı italic OCR + altta "TANIM:", "CEVAP:" gibi bold etiketli paragraflar
- 🔍 **Gemini truncate log** — `finish_reason=MAX_TOKENS` durumu loglanır, "yarıda kesildi mi" sorusu artık net cevaplanabilir

### v1.0.4
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
