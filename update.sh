#!/usr/bin/env bash
#
# Kitabi — Güncelleme scripti
#
# Cloud Shell'de yeni sürümü çekip Cloud Run'a redeploy eder.
# install.sh'ten sonra her güncellemede tek komut yeterli.
#
# Kullanım (Cloud Shell):
#   curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/update.sh | bash

set -euo pipefail

if [ -t 1 ]; then
    C_GRN='\033[0;32m'; C_BLU='\033[0;34m'; C_RED='\033[0;31m'
    C_YEL='\033[0;33m'; C_CYN='\033[0;36m'; C_BLD='\033[1m'; C_RST='\033[0m'
else
    C_GRN='' C_BLU='' C_RED='' C_YEL='' C_CYN='' C_BLD='' C_RST=''
fi

say()  { printf "%b\n" "$*"; }
ok()   { say "${C_GRN}✓${C_RST}  $*"; }
err()  { say "${C_RED}✗${C_RST}  $*" >&2; }
step() { say "\n${C_BLU}${C_BLD}━━━ $* ━━━${C_RST}"; }

REPO_URL="${KITABI_REPO_URL:-https://github.com/poeple-app/kitabi.git}"
REGION="${KITABI_REGION:-europe-west1}"

say "${C_CYN}${C_BLD}Kitabi — güncelleme${C_RST}"

# 1. Project doğrula
PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    err "Aktif GCP project bulunamadı. gcloud config set project <PROJECT_ID>"
    exit 1
fi
ok "Aktif proje: ${C_BLD}${PROJECT_ID}${C_RST}"

# 2. Mevcut Cloud Run servisi var mı?
if ! gcloud run services describe kitabi --region="$REGION" \
    --project="$PROJECT_ID" >/dev/null 2>&1; then
    err "Bu projede 'kitabi' adında Cloud Run servisi yok."
    say "Önce install.sh çalıştırman gerekiyor:"
    say "  ${C_CYN}curl -sL https://raw.githubusercontent.com/poeple-app/kitabi/main/install.sh | bash${C_RST}"
    exit 1
fi
ok "Mevcut servis bulundu"

# 3. Kaynak kodu çek
step "Kaynak kod"
TMPDIR="${HOME}/kitabi-update-$$"
say "Repo: $REPO_URL"
git clone --depth 1 "$REPO_URL" "$TMPDIR" >/dev/null 2>&1
ok "İndirildi"
cd "$TMPDIR"

# Mevcut versiyonu göster
if [ -f pyproject.toml ]; then
    new_ver=$(grep -E '^version' pyproject.toml | head -1 | cut -d'"' -f2 || true)
    [ -n "$new_ver" ] && ok "Yeni sürüm: ${C_BLD}v${new_ver}${C_RST}"
fi

# Çalışan sürümü öğren
running_ver=$(curl -s "$(gcloud run services describe kitabi --region="$REGION" \
    --project="$PROJECT_ID" --format='value(status.url)')/healthz" \
    2>/dev/null | grep -oE '"version":"[^"]*"' | cut -d'"' -f4 || true)
[ -n "$running_ver" ] && ok "Şu an çalışan: ${C_BLD}v${running_ver}${C_RST}"

# 4. Deploy
step "Cloud Run'a redeploy (≈3 dk)"
gcloud run deploy kitabi \
    --source . \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --quiet

# 5. Sonuç
SERVICE_URL=$(gcloud run services describe kitabi \
    --region="$REGION" --project="$PROJECT_ID" \
    --format='value(status.url)')

cd "$HOME"
rm -rf "$TMPDIR"

step "Tamam"
ok "Servis çalışıyor: ${C_BLD}${SERVICE_URL}${C_RST}"
say ""
say "Loglar: https://console.cloud.google.com/run/detail/${REGION}/kitabi/logs?project=${PROJECT_ID}"
say ""
