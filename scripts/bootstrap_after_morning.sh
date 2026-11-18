set -euo pipefail
now_hhmm="$(date +%H%M)"
if [ "${now_hhmm}" -lt 0600 ] || [ "${now_hhmm}" -gt 2055 ]; then
  echo "G11_PUBLIC_PREDEADLINE_BOOTSTRAP=OUTSIDE_WINDOW"
  exit 0
fi
test "${OPERATIONAL_DATE}" = "$(date +%F)"
curl --fail-with-body --silent --show-error --location \
  "${G11_APP_ORIGIN}/api/g11-feed?date=${OPERATIONAL_DATE}" \
  -o .relay-output/bootstrap-morning-readback.json
jq -e --arg day "${OPERATIONAL_DATE}" '
  .operational_date_jst == $day and .status == "PASS"
  and (.stage == "MORNING" or .stage == "PREDEADLINE")
  and .canonical_authorities.growth_p3.status == "LOCKED"
  and .canonical_authorities.growth_p3.app_readback_status == "PASS"
' .relay-output/bootstrap-morning-readback.json >/dev/null
for status in queued in_progress waiting pending requested; do
  runs="$(curl --fail-with-body --silent --show-error \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/g11-free-runner.yml/runs?status=${status}&per_page=100")"
  active="$(printf '%s' "${runs}" | jq --argjson current "${GITHUB_RUN_ID}" \
    '[.workflow_runs[] | select(.id != $current)] | length')"
  if [ "${active}" -gt 0 ]; then
    echo "G11_PUBLIC_PREDEADLINE_BOOTSTRAP=ACTIVE_${status}"
    exit 0
  fi
done
python3 scripts/predeadline_dispatch_v1.py --day "${OPERATIONAL_DATE}" --mode bootstrap
