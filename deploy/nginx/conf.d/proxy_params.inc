# M13 Phase 3: shared proxy directives for every location that forwards
# to the backend. This is the ONE place forwarded headers are set —
# `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for` APPENDS
# to whatever a client already sent rather than replacing it, which is
# the standard nginx idiom but would let a client prepend a fake chain;
# using bare `$remote_addr` instead means nginx always sets this header
# to the value IT observed the TCP peer to be, discarding whatever a
# client supplied — the client cannot inject a spoofed value that
# survives this hop (verified: deploy/tests/test_proxy_tls.py).
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;

# A client-supplied X-Request-ID is forwarded (M12's correlation-ID
# middleware treats it as an optional hint, bounded in length — see
# app/core/correlation.py); nginx never invents its own separate ID, so
# there is exactly one ID per request end to end (M13 Phase 13).
proxy_set_header X-Request-ID $http_x_request_id;

proxy_http_version 1.1;
proxy_set_header Connection "";

proxy_connect_timeout 60s;
proxy_send_timeout    60s;
proxy_read_timeout    60s;

proxy_buffering on;
proxy_buffer_size 8k;
proxy_buffers 8 8k;
