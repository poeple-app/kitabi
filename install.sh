#!/usr/bin/env bash
#
# Kitabi — Hızlı kurulum scripti (v1.0.5)
#
# Bu script Google Cloud Shell'de çalışır. Yapacakları:
#   1.  Billing account seç
#   2.  Yeni GCP project oluştur ve billing'e bağla
#   3.  Gerekli 4 API'yi enable et
#   4.  Cloud Storage bucket yarat
#   5.  Telegram bot token + user ID + Gemini key topla
#   6.  Secret Manager'a 4 secret yaz
#   7.  Cloud Run service account'a gerekli IAM rollerini bağla
#   8.  Cloud Run'a deploy
#   9.  Webhook'u BOT_BASE_URL ile redeploy ve Telegram'a tanıt
#  10.  Bot URL'ini göster
#
# Tipik kullanım (Cloud Shell):
#   curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/install.sh | bash
#
# Kesintide (Ctrl+C) state dosyası ~/.kitabi-install-state'e kaydedilir.
# Tekrar çalıştırılırsa kaldığı yerden devam eder.

set -euo pipefail

# ─────────────── Renkler ───────────────
if [ -t 1 ]; then
    C_RED='\033[0;31m'
    C_GRN='\033[0;32m'
    C_YEL='\033[0;33m'
    C_BLU='\033[0;34m'
    C_MAG='\033[0;35m'
    C_CYN='\033[0;36m'
    C_BLD='\033[1m'
    C_DIM='\033[2m'
    C_RST='\033[0m'
else
    C_RED='' C_GRN='' C_YEL='' C_BLU='' C_MAG='' C_CYN='' C_BLD='' C_DIM='' C_RST=''
fi

say()       { printf "%b\n" "$*"; }
step()      { say "\n${C_BLU}${C_BLD}━━━ $* ━━━${C_RST}"; }
ok()        { say "${C_GRN}✓${C_RST}  $*"; }
warn()      { say "${C_YEL}⚠${C_RST}  $*"; }
err()       { say "${C_RED}✗${C_RST}  $*" >&2; }
info()      { say "${C_DIM}   $*${C_RST}"; }
ask()       { printf "${C_CYN}❯${C_RST}  %s " "$*"; }
banner()    {
    say "${C_MAG}${C_BLD}"
    say "  ╔══════════════════════════════════════════════════════════════╗"
    say "  ║                                                              ║"
    say "  ║       📚  Kitabi — Hızlı Kurulum  (v1.0.5)                   ║"
    say "  ║                                                              ║"
    say "  ║       Tahmini süre: ~10-15 dakika                            ║"
    say "  ║       Ücret: 0 TL (Google free tier kullanılır)              ║"
    say "  ║                                                              ║"
    say "  ╚══════════════════════════════════════════════════════════════╝"
    say "${C_RST}"
}

# ─────────────── State persistence ───────────────
STATE_FILE="${HOME}/.kitabi-install-state"
state_get() {
    local key="$1"
    [ -f "$STATE_FILE" ] || return 1
    grep -E "^${key}=" "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || return 1
}
state_set() {
    local key="$1"
    local val="$2"
    [ -f "$STATE_FILE" ] || touch "$STATE_FILE"
    # Replace if exists, else append
    if grep -qE "^${key}=" "$STATE_FILE"; then
        sed -i.bak "s|^${key}=.*|${key}=${val}|" "$STATE_FILE" && rm -f "${STATE_FILE}.bak"
    else
        echo "${key}=${val}" >> "$STATE_FILE"
    fi
}

# ─────────────── Önkoşul kontrolleri ───────────────
require_gcloud() {
    if ! command -v gcloud >/dev/null 2>&1; then
        err "gcloud CLI bulunamadı."
        info "Bu scripti Google Cloud Shell'de çalıştırmalısın:"
        info "  https://shell.cloud.google.com"
        info "Cloud Shell'de gcloud önceden kurulu gelir."
        exit 1
    fi
    ok "gcloud CLI bulundu ($(gcloud --version | head -1))"
}

require_login() {
    local account
    account=$(gcloud config get-value account 2>/dev/null || true)
    if [ -z "$account" ] || [ "$account" = "(unset)" ]; then
        err "Google hesabına giriş yapılmamış."
        info "Şu komutu çalıştır, sonra scripti tekrar başlat:"
        info "  gcloud auth login"
        exit 1
    fi
    ok "Google hesabı: ${C_BLD}${account}${C_RST}"
}

# ─────────────── Billing seçimi ───────────────
choose_billing() {
    step "1/9 — Billing hesabı"

    local cached
    cached=$(state_get BILLING_ACCOUNT || true)
    if [ -n "${cached:-}" ]; then
        ok "Önbellekteki billing hesabı kullanılacak: ${C_BLD}${cached}${C_RST}"
        BILLING_ACCOUNT="$cached"
        return
    fi

    say "Mevcut billing hesaplarını listeliyorum..."
    local list
    list=$(gcloud billing accounts list --filter='open=true' \
        --format='value(name,displayName)' 2>/dev/null || true)

    if [ -z "$list" ]; then
        warn "Açık billing hesabın yok."
        say ""
        say "Google Cloud'a kart eklemen gerekiyor (ücretsiz katmanı için zorunlu, çekim yapılmaz)."
        say ""
        say "  1. Bu sekmeyi açık bırak."
        say "  2. Yeni sekme aç: ${C_CYN}https://console.cloud.google.com/billing${C_RST}"
        say "  3. 'Hesap ekle' / 'Add billing account' ile kart bilgilerini gir."
        say "  4. Bittiğinde buraya dön."
        say ""
        read -r -p "Bittiğinde Enter'a bas... " _
        list=$(gcloud billing accounts list --filter='open=true' \
            --format='value(name,displayName)' 2>/dev/null || true)
        if [ -z "$list" ]; then
            err "Hâlâ açık billing hesabı bulamadım. Yarım kalan kart eklemesi olabilir."
            exit 1
        fi
    fi

    local accounts=()
    local labels=()
    while IFS=$'\t' read -r name label; do
        accounts+=("$name")
        labels+=("$label")
    done <<< "$list"

    say ""
    say "Açık billing hesapların:"
    local i=1
    for label in "${labels[@]}"; do
        local id="${accounts[$((i-1))]}"
        id="${id#billingAccounts/}"
        printf "   ${C_CYN}%d)${C_RST}  %s  ${C_DIM}(%s)${C_RST}\n" "$i" "$label" "$id"
        i=$((i+1))
    done

    local choice
    while true; do
        ask "Hangi billing hesabını kullanayım? [1]:"
        read -r choice
        choice="${choice:-1}"
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#accounts[@]}" ]; then
            BILLING_ACCOUNT="${accounts[$((choice-1))]}"
            BILLING_ACCOUNT="${BILLING_ACCOUNT#billingAccounts/}"
            break
        fi
        warn "Geçersiz seçim, 1-${#accounts[@]} arası bir sayı gir."
    done

    state_set BILLING_ACCOUNT "$BILLING_ACCOUNT"
    ok "Billing seçildi: ${C_BLD}${BILLING_ACCOUNT}${C_RST}"
}

# ─────────────── Project oluştur ───────────────
create_project() {
    step "2/9 — GCP Project"

    local cached
    cached=$(state_get PROJECT_ID || true)
    if [ -n "${cached:-}" ]; then
        if gcloud projects describe "$cached" >/dev/null 2>&1; then
            ok "Önbellekteki proje kullanılacak: ${C_BLD}${cached}${C_RST}"
            PROJECT_ID="$cached"
            gcloud config set project "$PROJECT_ID" >/dev/null
            return
        else
            warn "Önbellekteki proje ($cached) bulunamadı, yeniden yaratılacak."
        fi
    fi

    ask "Proje adı yaz [kitabi-prod]:"
    read -r raw
    local base="${raw:-kitabi-prod}"
    # Globally unique olmalı — küçük random suffix
    local suffix
    suffix=$(date +%s | tail -c 6)
    PROJECT_ID="${base}-${suffix}"

    say "Yaratılıyor: ${C_BLD}${PROJECT_ID}${C_RST}"
    gcloud projects create "$PROJECT_ID" --name="Kitabi" >/dev/null
    gcloud config set project "$PROJECT_ID" >/dev/null
    ok "Proje yaratıldı"

    say "Billing'e bağlanıyor..."
    gcloud billing projects link "$PROJECT_ID" \
        --billing-account="$BILLING_ACCOUNT" >/dev/null
    ok "Billing bağlandı"

    state_set PROJECT_ID "$PROJECT_ID"
}

# ─────────────── API'leri enable et ───────────────
enable_apis() {
    step "3/9 — API'ler enable ediliyor (≈60 sn)"

    local apis=(
        "secretmanager.googleapis.com"
        "run.googleapis.com"
        "storage.googleapis.com"
        "artifactregistry.googleapis.com"
        "cloudbuild.googleapis.com"
    )

    for api in "${apis[@]}"; do
        printf "   %s ... " "$api"
        if gcloud services enable "$api" --project="$PROJECT_ID" >/dev/null 2>&1; then
            printf "${C_GRN}✓${C_RST}\n"
        else
            printf "${C_RED}✗${C_RST}\n"
            err "API enable edilemedi: $api"
            exit 1
        fi
    done
    ok "Tüm API'ler hazır"
}

# ─────────────── Manuel adımlar: Telegram + Gemini ───────────────
collect_manual_inputs() {
    step "4/9 — Üç manuel adım"

    say "Bu üç değeri tarayıcından alıp buraya yapıştır."
    say ""

    # Telegram token
    TELEGRAM_TOKEN=$(state_get TELEGRAM_TOKEN || true)
    if [ -z "${TELEGRAM_TOKEN:-}" ]; then
        say "${C_CYN}A) Telegram bot token${C_RST}"
        say "   1. Tarayıcıda aç: ${C_CYN}https://t.me/BotFather${C_RST}"
        say "   2. /newbot yaz, bot'a ad ver"
        say "   3. Token'ı kopyala (formatı: ${C_DIM}123456:ABC-DEF...${C_RST})"
        while true; do
            ask "Token:"
            read -r TELEGRAM_TOKEN
            if [[ "$TELEGRAM_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
                ok "Token geçerli"
                state_set TELEGRAM_TOKEN "$TELEGRAM_TOKEN"
                break
            fi
            warn "Format yanlış. Tipik token: 123456:ABCdef..."
        done
    else
        ok "Telegram token önbellekten alındı"
    fi

    say ""

    # Telegram user ID
    USER_ID=$(state_get USER_ID || true)
    if [ -z "${USER_ID:-}" ]; then
        say "${C_CYN}B) Telegram kullanıcı ID'n${C_RST}"
        say "   1. Tarayıcıda aç: ${C_CYN}https://t.me/userinfobot${C_RST}"
        say "   2. /start yaz, sana 'Id: 123456789' diye dönecek"
        say "   3. Sadece sayıyı kopyala"
        while true; do
            ask "User ID:"
            read -r USER_ID
            if [[ "$USER_ID" =~ ^[0-9]+$ ]] && [ ${#USER_ID} -ge 5 ]; then
                ok "ID geçerli"
                state_set USER_ID "$USER_ID"
                break
            fi
            warn "Sadece rakam olmalı, en az 5 hane."
        done
    else
        ok "User ID önbellekten alındı"
    fi

    say ""

    # Gemini API key
    GEMINI_KEY=$(state_get GEMINI_KEY || true)
    if [ -z "${GEMINI_KEY:-}" ]; then
        say "${C_CYN}C) Gemini API anahtarı${C_RST}"
        say "   1. Tarayıcıda aç: ${C_CYN}https://aistudio.google.com/app/apikey${C_RST}"
        say "   2. 'Create API key' butonu (varsa yeni proje seç)"
        say "   3. Key'i kopyala (formatı: ${C_DIM}AIza...${C_RST})"
        while true; do
            ask "Gemini key:"
            read -r GEMINI_KEY
            if [[ "$GEMINI_KEY" =~ ^AIza[A-Za-z0-9_-]{35}$ ]]; then
                ok "Key geçerli"
                state_set GEMINI_KEY "$GEMINI_KEY"
                break
            fi
            warn "Format yanlış. AIza ile başlayan 39 karakterli olmalı."
        done
    else
        ok "Gemini key önbellekten alındı"
    fi
}

# ─────────────── Webhook secret üret ───────────────
generate_webhook_secret() {
    WEBHOOK_SECRET=$(state_get WEBHOOK_SECRET || true)
    if [ -z "${WEBHOOK_SECRET:-}" ]; then
        WEBHOOK_SECRET=$(openssl rand -hex 32)
        state_set WEBHOOK_SECRET "$WEBHOOK_SECRET"
    fi
}

# ─────────────── Secret Manager'a yaz ───────────────
write_secrets() {
    step "5/9 — Secret Manager"

    declare -A secrets=(
        ["telegram-bot-token"]="$TELEGRAM_TOKEN"
        ["allowed-tg-user-ids"]="$USER_ID"
        ["gemini-api-key"]="$GEMINI_KEY"
        ["webhook-secret"]="$WEBHOOK_SECRET"
    )

    for key in "${!secrets[@]}"; do
        printf "   %s ... " "$key"
        if gcloud secrets describe "$key" --project="$PROJECT_ID" >/dev/null 2>&1; then
            printf "${C_DIM}(zaten var, version ekleniyor)${C_RST} "
            printf "%s" "${secrets[$key]}" | \
                gcloud secrets versions add "$key" --data-file=- \
                --project="$PROJECT_ID" >/dev/null
        else
            printf "%s" "${secrets[$key]}" | \
                gcloud secrets create "$key" --data-file=- \
                --replication-policy=automatic \
                --project="$PROJECT_ID" >/dev/null
        fi
        printf "${C_GRN}✓${C_RST}\n"
    done
    ok "4 secret yazıldı"
}

# ─────────────── Bucket yarat ───────────────
create_bucket() {
    step "6/9 — Cloud Storage bucket"

    BUCKET_NAME=$(state_get BUCKET_NAME || true)
    if [ -z "${BUCKET_NAME:-}" ]; then
        BUCKET_NAME="${PROJECT_ID}-db"
        state_set BUCKET_NAME "$BUCKET_NAME"
    fi

    if gcloud storage buckets describe "gs://${BUCKET_NAME}" \
        --project="$PROJECT_ID" >/dev/null 2>&1; then
        ok "Bucket zaten var: ${C_BLD}${BUCKET_NAME}${C_RST}"
    else
        gcloud storage buckets create "gs://${BUCKET_NAME}" \
            --location="$REGION" \
            --uniform-bucket-level-access \
            --public-access-prevention \
            --project="$PROJECT_ID" >/dev/null
        ok "Bucket yaratıldı: ${C_BLD}${BUCKET_NAME}${C_RST}"
    fi
}

# ─────────────── IAM rolleri ───────────────
bind_iam() {
    step "7/9 — IAM yetkileri"

    local project_number
    project_number=$(gcloud projects describe "$PROJECT_ID" \
        --format='value(projectNumber)')
    local sa="${project_number}-compute@developer.gserviceaccount.com"

    # Storage erişimi
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${sa}" \
        --role="roles/storage.objectAdmin" \
        --condition=None \
        --quiet >/dev/null
    ok "Cloud Run → Cloud Storage erişimi"

    # Secret okuma
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${sa}" \
        --role="roles/secretmanager.secretAccessor" \
        --condition=None \
        --quiet >/dev/null
    ok "Cloud Run → Secret Manager erişimi"

    state_set SERVICE_ACCOUNT "$sa"
}

# ─────────────── Cloud Run deploy ───────────────
deploy_service() {
    step "8/9 — Cloud Run'a deploy (≈3-4 dk)"

    # Kaynak kodu Cloud Shell'de yoksa indir
    if [ ! -f "./Dockerfile" ] || [ ! -d "./kitabi" ]; then
        say "Kaynak kodu indiriliyor..."
        local tmpdir="${HOME}/kitabi-source"
        rm -rf "$tmpdir"
        git clone --depth 1 "$REPO_URL" "$tmpdir" >/dev/null 2>&1
        cd "$tmpdir"
        ok "Kaynak hazır: $(pwd)"
    fi

    say "Build + deploy başlatılıyor (loglar aşağıda)..."
    gcloud run deploy kitabi \
        --source . \
        --region="$REGION" \
        --allow-unauthenticated \
        --memory=512Mi \
        --cpu=1 \
        --max-instances=1 \
        --timeout=300 \
        --set-env-vars="GCS_BUCKET_NAME=${BUCKET_NAME},DB_PATH=/data/kitabi.db,BOT_BASE_URL=" \
        --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,ALLOWED_TG_USER_IDS=allowed-tg-user-ids:latest,GEMINI_API_KEY=gemini-api-key:latest,WEBHOOK_SECRET=webhook-secret:latest" \
        --project="$PROJECT_ID" \
        --quiet

    SERVICE_URL=$(gcloud run services describe kitabi \
        --region="$REGION" \
        --project="$PROJECT_ID" \
        --format='value(status.url)')
    ok "Servis çalışıyor: ${C_BLD}${SERVICE_URL}${C_RST}"
    state_set SERVICE_URL "$SERVICE_URL"
}

# ─────────────── Webhook bağla ───────────────
setup_webhook() {
    step "9/9 — Webhook bağlanıyor"

    # BOT_BASE_URL'i güncelle + redeploy
    say "BOT_BASE_URL set ediliyor..."
    gcloud run services update kitabi \
        --region="$REGION" \
        --update-env-vars="BOT_BASE_URL=${SERVICE_URL}" \
        --project="$PROJECT_ID" \
        --quiet >/dev/null
    ok "BOT_BASE_URL güncellendi (otomatik webhook için)"

    # Manuel webhook set (lifespan da yapar ama burada da garanti edelim)
    say "Telegram'a webhook tanıtılıyor..."
    local resp
    resp=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
        -d "url=${SERVICE_URL}/webhook" \
        -d "secret_token=${WEBHOOK_SECRET}" \
        -d "drop_pending_updates=true")
    if echo "$resp" | grep -q '"ok":true'; then
        ok "Webhook bağlandı"
    else
        warn "Webhook set başarısız olabilir. Telegram cevabı:"
        info "$resp"
    fi
}

# ─────────────── Bitiş özeti ───────────────
print_summary() {
    say ""
    say "${C_GRN}${C_BLD}"
    say "  ╔══════════════════════════════════════════════════════════════╗"
    say "  ║                                                              ║"
    say "  ║       🎉  Kurulum tamamlandı!                                ║"
    say "  ║                                                              ║"
    say "  ╚══════════════════════════════════════════════════════════════╝"
    say "${C_RST}"
    say ""
    say "  ${C_BLD}Sonraki adım:${C_RST}"
    say "    Telegram'da botu aç ve ${C_CYN}/start${C_RST} yaz."
    say ""
    say "  ${C_BLD}Yararlı linkler:${C_RST}"
    say "    📊  Loglar:   ${C_DIM}https://console.cloud.google.com/run/detail/${REGION}/kitabi/logs?project=${PROJECT_ID}${C_RST}"
    say "    ⚙️  Servis:   ${C_DIM}https://console.cloud.google.com/run/detail/${REGION}/kitabi?project=${PROJECT_ID}${C_RST}"
    say "    🔐  Secrets:  ${C_DIM}https://console.cloud.google.com/security/secret-manager?project=${PROJECT_ID}${C_RST}"
    say ""
    say "  ${C_BLD}Güncelleme yapmak istediğinde:${C_RST}"
    say "    Cloud Shell'i tekrar aç ve şunu çalıştır:"
    say "    ${C_CYN}curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/update.sh | bash${C_RST}"
    say ""
    say "  ${C_BLD}Sorun mu var?${C_RST}"
    say "    TROUBLESHOOTING.md veya issue: github.com/poeple-app/kitabi/issues"
    say ""
}

# ─────────────── Main ───────────────
main() {
    REPO_URL="${KITABI_REPO_URL:-https://github.com/poeple-app/kitabi.git}"
    REGION="${KITABI_REGION:-europe-west1}"

    banner

    say "Bu script Cloud Shell'de çalışır. Yapacaklarımız:"
    say "  ${C_DIM}• GCP project ve gerekli servisleri oluştur${C_RST}"
    say "  ${C_DIM}• Telegram bot + Gemini bilgilerini topla (sen vereceksin)${C_RST}"
    say "  ${C_DIM}• Cloud Run'a deploy et${C_RST}"
    say "  ${C_DIM}• Webhook'u bağla${C_RST}"
    say ""
    ask "Devam edelim mi? [E/h]:"
    read -r yn
    case "${yn:-E}" in
        H|h|N|n) say "İptal edildi."; exit 0 ;;
    esac

    require_gcloud
    require_login

    choose_billing
    create_project
    enable_apis
    collect_manual_inputs
    generate_webhook_secret
    write_secrets
    create_bucket
    bind_iam
    deploy_service
    setup_webhook
    print_summary

    # Başarılı bittiğinde state dosyasını sil
    rm -f "$STATE_FILE"
}

main "$@"
