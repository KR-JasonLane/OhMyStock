#!/usr/bin/env sh

# Production nginx routing contract.  The fake backend deliberately echoes the
# received URI so this test proves that the public /api prefix is removed.
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$repo_root"

image="ohmystock-frontend:local"
network="ohmystock-frontend-routing-network-$$"
backend_container="ohmystock-frontend-routing-backend-$$"
frontend_container="ohmystock-frontend-routing-frontend-$$"
requester_container="ohmystock-frontend-routing-requester-$$"
fixture_dir=$(mktemp -d)

cleanup() {
  docker rm -f "$requester_container" "$frontend_container" "$backend_container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -rf "$fixture_dir"
}
trap cleanup EXIT INT TERM

docker compose build frontend
docker network create "$network" >/dev/null

cat >"$fixture_dir/default.conf" <<'EOF'
server {
    listen 8000;
    server_name _;
    default_type application/json;

    location = /dashboard/overview {
        if ($arg_backend_status = 503) {
            return 503 '{"source":"backend unavailable"}\n';
        }
        return 200 '{"path":"$uri","query":"$args"}\n';
    }

    location / {
        return 404 '{"source":"backend not found"}\n';
    }
}
EOF

docker run --detach --rm --name "$backend_container" --network "$network" \
  --network-alias backend \
  --volume "$fixture_dir/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:1.31.3-alpine@sha256:1d40e3eb3bf4f138de1d67193f2aa5309fcaf343eb5ffadbf5e9439de1eb1ebb >/dev/null

backend_ready=false
for _ in 1 2 3 4 5; do
  if docker exec "$backend_container" wget -q -O /dev/null http://127.0.0.1:8000/dashboard/overview; then
    backend_ready=true
    break
  fi
  sleep 1
done
if [ "$backend_ready" != true ]; then
  echo "fake backend did not become ready" >&2
  docker logs "$backend_container" >&2 || true
  exit 1
fi

docker run --detach --rm --name "$frontend_container" --network "$network" \
  "$image" >/dev/null

frontend_ready=false
for _ in 1 2 3 4 5; do
  if docker exec "$frontend_container" wget -q -O /dev/null http://127.0.0.1:8080/; then
    frontend_ready=true
    break
  fi
  sleep 1
done
if [ "$frontend_ready" != true ]; then
  echo "frontend did not become ready" >&2
  docker logs "$frontend_container" >&2 || true
  exit 1
fi

frontend_ip=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$frontend_container")
if [ -z "$frontend_ip" ]; then
  echo "frontend container has no network IP" >&2
  exit 1
fi
frontend_url="http://$frontend_ip:8080"

docker run --detach --rm --name "$requester_container" --network "$network" \
  --entrypoint sh \
  curlimages/curl:8.12.1@sha256:88a9abad9d958340e48564f9bdcdaa29916a2984be59314da709f8bbc0eef6f7 \
  -c 'while :; do sleep 3600; done' >/dev/null

request() {
  method=$1
  path=$2
  headers=$fixture_dir/headers
  body=$fixture_dir/body
  curl --silent --show-error --request "$method" --dump-header "$headers" --output "$body" \
    --write-out '%{http_code}' "$frontend_url$path"
}

assert_status() {
  expected=$1
  method=$2
  path=$3
  actual=$(request "$method" "$path")
  if [ "$actual" != "$expected" ]; then
    echo "expected $method $path to return $expected, got $actual" >&2
    cat "$fixture_dir/body" >&2
    exit 1
  fi
}

assert_content_type() {
  expected=$1
  actual=$(header_value 'content-type:')
  case "$actual" in
    "$expected"*) ;;
    *)
      echo "expected content type $expected, got ${actual:-missing}" >&2
      exit 1
      ;;
  esac
}

header_value() {
  expected=$1
  awk -v expected="$expected" '
    tolower($1) == tolower(expected) {
      sub(/^[^:]*:[[:space:]]*/, "")
      sub(/\r$/, "")
      print
      exit
    }
  ' "$fixture_dir/headers"
}

assert_header_contains() {
  header=$1
  expected=$2
  actual=$(header_value "$header")
  case "$actual" in
    *"$expected"*) ;;
    *)
      echo "expected $header to contain $expected, got ${actual:-missing}" >&2
      exit 1
      ;;
  esac
}

assert_security_headers() {
  assert_header_contains 'content-security-policy:' "default-src 'self'"
  assert_header_contains 'x-content-type-options:' nosniff
  assert_header_contains 'referrer-policy:' no-referrer
}

assert_body_contains() {
  expected=$1
  if ! grep -Fq "$expected" "$fixture_dir/body"; then
    echo "response body did not contain: $expected" >&2
    cat "$fixture_dir/body" >&2
    exit 1
  fi
}

assert_status 200 GET /
assert_content_type text/html
assert_security_headers
assert_status 200 GET /portfolio/open-positions
assert_content_type text/html
assert_security_headers

assert_status 200 GET '/api/dashboard/overview?from=2026-07-01&to=2026-07-27&timezone=Asia%2FSeoul'
assert_content_type application/json
assert_security_headers
assert_body_contains '"path":"/dashboard/overview"'
assert_body_contains '"query":"from=2026-07-01&to=2026-07-27&timezone=Asia%2FSeoul"'

assert_status 405 POST /api/dashboard/overview
assert_security_headers
assert_status 404 GET /api/trade/start
assert_security_headers
assert_status 404 GET /api/trade/stop
assert_security_headers
assert_status 404 GET /api/schedule/status
assert_security_headers
assert_status 404 GET /ws
assert_security_headers

assert_status 503 GET '/api/dashboard/overview?backend_status=503'
assert_content_type application/json
assert_security_headers
assert_body_contains '"source":"backend unavailable"'

# A newly-created fixed source has no earlier request.  Seven simultaneous
# requests therefore deterministically exercise a 6-request burst: exactly six
# pass and exactly one is locally rejected before reaching the fake backend.
docker exec "$requester_container" sh -c '
  target=$1
  for request_index in 1 2 3 4 5 6 7; do
    curl --silent --show-error --request GET \
      --dump-header "/tmp/rate-$request_index.headers" \
      --output "/tmp/rate-$request_index.body" --write-out "%{http_code}" \
      "$target" >"/tmp/rate-$request_index.status" &
  done
  wait
' sh "http://$frontend_container:8080/api/dashboard/overview"

rate_successes=0
rate_limited=0
rate_limited_index=
for request_index in 1 2 3 4 5 6 7; do
  docker cp "$requester_container:/tmp/rate-$request_index.status" "$fixture_dir/rate-$request_index.status"
  actual=$(tr -d '\r\n' <"$fixture_dir/rate-$request_index.status")
  case "$actual" in
    200) rate_successes=$((rate_successes + 1)) ;;
    429)
      rate_limited=$((rate_limited + 1))
      rate_limited_index=$request_index
      ;;
    *)
      echo "expected concurrent rate request to return 200 or 429, got ${actual:-missing}" >&2
      exit 1
      ;;
  esac
done
if [ "$rate_successes" -ne 6 ] || [ "$rate_limited" -ne 1 ]; then
  echo "expected six 200 responses and one 429, got $rate_successes 200 and $rate_limited 429" >&2
  exit 1
fi

docker cp "$requester_container:/tmp/rate-$rate_limited_index.headers" "$fixture_dir/headers"
docker cp "$requester_container:/tmp/rate-$rate_limited_index.body" "$fixture_dir/body"
assert_content_type application/json
assert_security_headers
assert_body_contains '"detail":"dashboard request rate limit exceeded"'
retry_after=$(header_value 'retry-after:')
if [ "$retry_after" != "1" ]; then
  echo "expected Retry-After: 1, got ${retry_after:-missing}" >&2
  exit 1
fi

echo "nginx routing contract passed"
