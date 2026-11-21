# Private VPN and local hosting

OnionSurge can publish immutable hourly releases strictly to a private host directory (`LOCAL_ENABLED="true"`) instead of public DigitalOcean Spaces, R2, or Cloudflare Workers. Because the live UI checks SHA-256 checksums using `crypto.subtle`, serve browsers over a secure origin (`https://` on your tailnet/VPN, or SSH port-forwarded `http://127.0.0.1:8781/`).

## Configure

Set the local hosting keys in mode-`0600` `config.env`:

```sh
DO_ENABLED="false"
R2_ENABLED="false"
LOCAL_ENABLED="true"
LOCAL_SITE_DIR="/srv/onionsurge"
LOCAL_BIND_ADDR="127.0.0.1:8781"
LOCAL_TLS_BIND_ADDR=""
LOCAL_URL_PREFIX="/"
LOCAL_RETAIN_RELEASES="3"
```

- `LOCAL_SITE_DIR` must stay outside `APPLICATION_DIR`, `onionsurge-deploy`, `STATE_DIR`, `LOG_DIR`, `SOURCE_CONFIG`, and any `TIME_RANGES_CONFIG`. Keep `STATE_DIR` mode `0700`. Create `LOCAL_SITE_DIR` before the first publication, owned by the publisher user with group `caddy` and mode `2750` (`sudo usermod -aG caddy <publisher-user>`, `sudo chgrp -R caddy <LOCAL_SITE_DIR>`, `sudo chmod 2750 <LOCAL_SITE_DIR>`) so newly created subdirectories and files (`0750`/`0640`) inherit group `caddy` (existing directories keep their prior group).
- `LOCAL_BIND_ADDR` and optional `LOCAL_TLS_BIND_ADDR` accept only loopback, RFC 1918, ULA, or Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) `IP:PORT` addresses. Public, unspecified (`0.0.0.0`, `::`), and hostname binds are refused.
- Set `LOCAL_TLS_BIND_ADDR="100.x.y.z:443"` if Tailscale MagicDNS/HTTPS is disabled (or `"100.x.y.z:8443"` when another service already binds `:443`). Standalone `--print-caddy`/`--write-caddy` (without `--no-global`) emits a global `auto_https disable_redirects` block so the `tls internal` IP site does not open an HTTP redirect listener on port 80; use `--no-global` when importing into an existing `/etc/caddy/Caddyfile` or into a persistent isolated-instance wrapper Caddyfile.
- Because `tls internal` signs using Caddy's local internal CA (for example `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt`), copy `root.crt` to each client machine and add it to the client's system/browser trust store before opening the IP-only HTTPS URL so TLS verification and `crypto.subtle` succeed:

  ```sh
  # On macOS client:
  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt
  # On Debian/Ubuntu client:
  sudo install -o root -g root -m 0644 root.crt /usr/local/share/ca-certificates/caddy-onionsurge.crt && sudo update-ca-certificates
  ```

- `LOCAL_URL_PREFIX` defaults to `"/"` (root) or accepts `"/slug"`.

### Sharing Caddy with existing sites when `LOCAL_TLS_BIND_ADDR` is set

- **Isolated Caddy instance via persistent wrapper (recommended when existing sites use automatic HTTPS redirects):**
  - Generate only the site/snippet fragment with `--no-global` to a publisher-writable path outside `/etc/caddy-onionsurge` (for example `--write-caddy /path/to/private/onionsurge-private.caddy --no-global`), then install it into `/etc/caddy-onionsurge/onionsurge-private.caddy` with `sudo install` after each rollout (Step 4b) so rollouts never overwrite persistent global options.
  - To avoid overlapping a wildcard `:443` listener on the primary Caddy instance (which causes `SO_REUSEPORT` traffic capture or `EADDRINUSE`), set a distinct port such as `LOCAL_TLS_BIND_ADDR="100.x.y.z:8443"` (clients then connect to `https://100.x.y.z:8443/`), or bind existing primary-instance sites to their own explicit non-tailnet IPs.
  - Create a persistent `/etc/caddy-onionsurge/Caddyfile` (`root:caddy`, mode `0640`) with a separate private `admin` endpoint (so it does not collide with the primary `localhost:2019` admin socket) and import the generated fragment:

  ```caddyfile
  {
      admin 127.0.0.1:2020
      auto_https disable_redirects
  }

  import /etc/caddy-onionsurge/onionsurge-private.caddy
  ```

- **Importing into `/etc/caddy/Caddyfile`:** Generate the fragment with `--no-global` and add `import /etc/caddy/onionsurge-private.caddy` after existing site blocks. If `LOCAL_TLS_BIND_ADDR` is set, add `auto_https disable_redirects` on its own line inside the existing top global block, or prepend this multiline global block at the top of `/etc/caddy/Caddyfile` if none exists:

  ```caddyfile
  {
      auto_https disable_redirects
  }
  ```

  Because `auto_https disable_redirects` affects every site in `/etc/caddy/Caddyfile`, any existing HTTPS site in that file needing HTTP-to-HTTPS redirects must declare an explicit HTTP redirect block.

## Stage, serve and verify

```sh
# 1. Run collection, live build and immutable data publication into LOCAL_SITE_DIR/data
python3 scripts/run_hourly.py --config /path/to/private/config.env

# 2. Prepare live frontend assets from the completed run build directory
python3 scripts/prepare_assets.py \
  --source /path/to/state/runs/RUN_ID/build --live \
  --application-commit FULL_40_CHARACTER_COMMIT_SHA \
  --output /path/to/state/frontends/local-release

# 3. Activate frontend atomically and write the Caddy config
#    (pass --no-global when using an isolated wrapper or importing into /etc/caddy/Caddyfile)
python3 scripts/local_site.py \
  --config /path/to/private/config.env \
  --frontend-release /path/to/state/frontends/local-release \
  --write-caddy /path/to/private/onionsurge-private.caddy

# 4a. Standalone host (replaces /etc/caddy/Caddyfile):
sudo install -o root -g root -m 0644 /path/to/private/onionsurge-private.caddy /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload-or-restart caddy

# 4b. Isolated Caddy instance (leaves primary /etc/caddy/Caddyfile and service untouched):
sudo install -d -o root -g caddy -m 0750 /etc/caddy-onionsurge
sudo install -o root -g caddy -m 0640 /path/to/private/onionsurge-private.caddy /etc/caddy-onionsurge/onionsurge-private.caddy
sudo caddy validate --config /etc/caddy-onionsurge/Caddyfile --adapter caddyfile
# Create boot-enabled unit /etc/systemd/system/caddy-onionsurge.service
# (replace 100.x.y.z with your LOCAL_TLS_BIND_ADDR IP so startup waits for it):
#   [Unit]
#   After=network-online.target tailscaled.service
#   Wants=network-online.target tailscaled.service
#   [Service]
#   User=caddy
#   Group=caddy
#   ExecStartPre=/bin/sh -c 'i=0; until ip -o addr show | grep -qF " 100.x.y.z/"; do i=$((i + 1)); [ "$i" -ge 30 ] && exit 1; sleep 1; done'
#   ExecStart=/usr/bin/caddy run --environ --config /etc/caddy-onionsurge/Caddyfile --adapter caddyfile
#   ExecReload=/usr/bin/caddy reload --config /etc/caddy-onionsurge/Caddyfile --adapter caddyfile --address 127.0.0.1:2020 --force
#   AmbientCapabilities=CAP_NET_BIND_SERVICE
#   Restart=on-failure
#   RestartSec=5s
#   [Install]
#   WantedBy=multi-user.target
sudo systemctl daemon-reload
sudo systemctl enable --now caddy-onionsurge
# Subsequent reloads against the isolated admin endpoint:
sudo -u caddy caddy reload --config /etc/caddy-onionsurge/Caddyfile --adapter caddyfile --address 127.0.0.1:2020

# 5. Verify live HTTP headers, manifest chain, and data digest
python3 scripts/local_site.py \
  --config /path/to/private/config.env \
  --verify-url http://127.0.0.1:8781/
```

With Tailscale MagicDNS HTTPS enabled, expose the loopback Caddy listener exclusively inside your tailnet (`Funnel` disabled):

```sh
sudo tailscale serve --bg http://127.0.0.1:8781
```
