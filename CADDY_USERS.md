# Adding operators to the basicauth (Caddyfile)

The human pages — `/dash`, `/ops`, `/calib-*`, `/floorcheck`, `/validate`, `/calibrate` — sit behind
Caddy basicauth. The token APIs (`/api/gw/*`) must NOT, because the Pi and the GPU carry a Bearer
token and have no browser to answer a password prompt.

## Add a user

One line per person, `username hash`. Generate the hash on the VM:

```bash
caddy hash-password --plaintext 'the-password-you-will-hand-over'
# -> $2a$14$....  (bcrypt; copy the WHOLE string including the $2a$14$ prefix)
```

Then in the Caddyfile:

```caddyfile
lift.gargi.online {
    # ---- token APIs: NO basicauth (the Pi/GPU authenticate with Bearer) ----
    @tokenapi path /api/gw/* /live/*
    handle @tokenapi {
        reverse_proxy 127.0.0.1:8000
    }

    # ---- human pages: basicauth, one line per operator ----
    handle {
        basic_auth {
            ajit     $2a$14$REPLACE_WITH_HASH_FOR_AJIT
            operator $2a$14$REPLACE_WITH_HASH_FOR_OPERATOR
            surveyor $2a$14$REPLACE_WITH_HASH_FOR_SURVEYOR
        }
        reverse_proxy 127.0.0.1:8000
    }
}
```

Reload without dropping connections:

```bash
caddy validate --config /etc/caddy/Caddyfile     # parse first — a bad file takes the site down
sudo systemctl reload caddy
```

## Things that bite

- **`basic_auth` vs `basicauth`.** Caddy v2.7+ renamed it; older config uses `basicauth`. `caddy
  validate` tells you which this build wants. Using the wrong one fails the reload, and if you
  restarted instead of reloading, the site is down until it is fixed.
- **Hashes contain `$`.** In a Caddyfile they are literal — do not quote or escape them. If you
  template the Caddyfile through a shell or env substitution, `$2a` will be eaten as a variable.
- **The matcher must not swallow `/api/gw/*`.** If basicauth covers everything, the GPU fleet's
  registry poll and the Pi's segment PUTs get 401. The fleet then changes nothing and logs it (by
  design), so the symptom is "no cameras ever start" rather than an obvious auth error.
- **Removing a user** is deleting their line + reload. There are no sessions to expire — basicauth
  is per-request — so access stops immediately.
- **Everyone shares one audit trail: none.** basicauth gives Caddy the username, but nothing
  downstream records who clicked what. The wizard's writes (`roi.json`, `labels.json`, cells) are
  attributed to nobody. If it matters who changed a camera's geometry, that needs the username
  passed through as a header and written into the JSON — say so and it can be added.
