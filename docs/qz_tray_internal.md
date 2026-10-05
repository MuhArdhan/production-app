# QZ Tray internal signing (two Windows workstations)

The Production App label printer uses QZ Tray's signed requests. The signing
certificate is separate from the HTTPS certificate for the Frappe website.

## Server files

For each Frappe site, place the pair under
`sites/<site>/private/qz_signing/`:

- `digital-certificate.txt` — public X.509 PEM certificate.
- `private-key.pem` — matching unencrypted RSA 2048-bit PKCS#8 private key.

The private key must stay outside Git, `public/`, and `private/files/`. Restrict
its directory to the server account (`0700`) and the key to `0600`. Back up
the pair together in the company's protected backup system. If a deployment
uses another site, copy the pair to that site's private directory before using
the label printer. The app does not create keys automatically during requests.

The API reads the public certificate for a permitted, confirmed Work Order.
For each QZ call, it signs QZ's SHA-256 request hash with RSA/SHA512. Frappe
requires a logged-in session, Work Order read permission, and a confirmed
Pre-Packing result; the signature endpoint also requires POST and CSRF.

## Both Windows PCs

1. Install/start QZ Tray 2.1 or newer on each PC.
2. Download public certificate `/qz-certificate`
3. Copy the public certificate to `C:\Program Files\QZ Tray\override.crt`
   using administrator privileges. Use the same `override.crt` on both PCs.
4. Exit and restart QZ Tray. Open Production App and print one confirmed
   Work Order label. On the initial trust prompt, check the certificate
   identity and choose **Allow** and **Remember this decision**.

Only the public certificate goes to the PCs. Never copy `private-key.pem` to
either workstation or a browser asset. If the private key is replaced, replace
the certificate on both PCs and the server as one change.

The public installation copy for this workspace is
`/workspace/development/qz-tray-setup/override.crt`. Its SHA-256 fingerprint
is `E1:62:6F:4F:22:16:E7:1E:E2:19:1E:57:C7:CF:47:D5:65:EA:42:1A:39:F4:81:94:F2:68:33:B6:22:D7:6F:0D`.

Logged-in users can download the active site's public certificate from
`/qz-certificate`. This redirects to the download endpoint and returns only
`override.crt`; the private key is never included.

The current certificate expires on 4 October 2031 UTC. Replace it before
then. The local server side test does not prove QZ Tray trust on the Windows
PCs; verify a physical label on both PCs after installation.
