# Callback HMAC signing

Outbound reply callbacks and Claude permission relay requests can carry a
dedicated HMAC signature. Set `WAKE_HMAC_SECRET` for both processes:

```bash
export WAKE_HMAC_SECRET='<current-key>,<previous-key>'
```

Keys are ordered. Senders sign with the first key and receivers accept any key,
which permits overlap during rotation. Values from `WAKE_HMAC_SECRET` are tried
before values from `wake.hmac_secret`; prefer the environment so secret material
does not enter `config.json`. With no configured key, outbound requests remain
unsigned. A receiver configured with `require_auth` must reject that request.

The request header is:

```text
X-Wake-Signature: t=<unix-seconds>,v1=<hex-hmac>
```

The signing input is `t + "." + sha256(raw_body).hexdigest()`, signed with
HMAC-SHA256. Receivers must reject malformed signatures, use
`hmac.compare_digest`, and reject timestamps more than 300 seconds from their
clock. A failed authenticated permission relay maps to HTTP 401. Reply callback
receivers should also deduplicate the accompanying `Idempotency-Key`.

`wake.hmac_secret` accepts either a comma-separated string or a list of strings
for deployments that deliberately keep this value in their local, uncommitted
configuration.
