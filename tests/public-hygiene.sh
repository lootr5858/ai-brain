#!/usr/bin/env bash

client_key='client_'"secret"
access_key='access_'"token"
refresh_key='refresh_'"token"
id_key='id_'"token"
token_key='tok'"en"
quote="[\"']"
secret_value='[A-Za-z0-9._~-]{16,}'
secret_key="(${client_key}|${access_key}|${refresh_key}|${id_key}|${token_key})"
bad_named_secret="(^|[^A-Za-z0-9_])${quote}?${secret_key}${quote}?[[:space:]]*[:=][[:space:]]*${quote}${secret_value}${quote}"
bad_bearer="Authorization[[:space:]]*:[[:space:]]*Bearer[[:space:]]+${secret_value}"
bad_role_secret="BRAIN_[A-Z0-9_]*${token_key^^}[[:space:]]*=[[:space:]]*${secret_value}"
bad_query_secret="[?&]${token_key}=${secret_value}"
PUBLIC_HYGIENE_CREDENTIAL_LEAK="$bad_named_secret|$bad_bearer|$bad_role_secret|$bad_query_secret"

public_hygiene_check_controls() {
  local fixture
  for fixture in \
    "${client_key}=\"abcdefghijklmnop\"" \
    "\"${client_key}\": \"abcdefghijklmnop\"" \
    "${access_key}=\"abcdefghijklmnop\"" \
    "${refresh_key}=\"abcdefghijklmnop\"" \
    "${id_key}=\"abcdefghijklmnop\"" \
    "${token_key}='abcdefghijklmnop'" \
    "Authoriza""tion: Bearer abcdefghijklmnop" \
    "BRAIN_TEST_${token_key^^}=abcdefghijklmnop" \
    "?${token_key}=abcdefghijklmnop"
  do
    if ! printf '%s\n' "$fixture" |
      rg -q "$PUBLIC_HYGIENE_CREDENTIAL_LEAK"
    then
      echo "FAIL: credential hygiene misses a positive leak fixture" >&2
      return 1
    fi
  done
  for fixture in \
    "$client_key" \
    "$access_key" \
    "$refresh_key" \
    "$id_key" \
    "$token_key"
  do
    if printf '%s\n' "$fixture" |
      rg -q "$PUBLIC_HYGIENE_CREDENTIAL_LEAK"
    then
      echo "FAIL: credential hygiene rejects a field identifier" >&2
      return 1
    fi
  done
}
