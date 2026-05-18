#!/bin/sh
set -eu

auth_enabled="$(printf '%s' "${BASIC_AUTH_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')"
auth_realm="${BASIC_AUTH_REALM:-AAP Bridge}"
auth_user_file="${BASIC_AUTH_USER_FILE:-/etc/nginx/auth/.htpasswd}"
auth_conf="/etc/nginx/conf.d/basic_auth.conf"

mkdir -p /etc/nginx/conf.d

case "$auth_enabled" in
  1|true|yes|on)
    if [ ! -s "$auth_user_file" ]; then
      echo "BASIC_AUTH_ENABLED is true but no htpasswd file was found at $auth_user_file" >&2
      exit 1
    fi
    cat >"$auth_conf" <<EOF
auth_basic "$auth_realm";
auth_basic_user_file $auth_user_file;
EOF
    ;;
  *)
    cat >"$auth_conf" <<'EOF'
auth_basic off;
EOF
    ;;
esac

exec nginx -g 'daemon off;'
