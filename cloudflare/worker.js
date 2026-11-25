// Cloudflare Worker for suc.<your-domain>.
// Serves only what Tesla needs from a public domain:
//   1) the public key that Fleet API checks during partner registration,
//   2) a static /callback page that shows the OAuth redirect address, so you can paste it
//      into the add-on panel in HA. The code in the address is useless without
//      client_secret, which only the add-on knows. This page sends it nowhere.
// Everything else returns 404.

const PUBLIC_KEY = `-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEL/d3hLuHMNg/08P6sLOSfonEZvU8
TrSEPYEo09azn6+N7MJba4swghQ0YarRSMkxCP46CLgfNPXivpSv4Dtrtg==
-----END PUBLIC KEY-----
`;

const CALLBACK_HTML = `<!doctype html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Tesla — kod logowania</title>
<style>
body{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.5}
textarea{width:100%;height:120px;font-family:monospace;font-size:13px}
button{padding:8px 16px;font-size:15px;margin-top:8px}
.err{color:#b00}
</style></head><body>
<h1>Prawie gotowe</h1>
<p id="msg">Skopiuj poniższy adres i wklej go w panelu dodatku <b>TeslaMate SuC</b> w Home Assistant.
Kod jest ważny kilka minut.</p>
<textarea id="u" readonly></textarea>
<button id="b">Kopiuj</button>
<script>
const u = document.getElementById('u');
u.value = location.href;
const p = new URLSearchParams(location.search);
if (p.get('error')) {
  const m = document.getElementById('msg');
  m.className = 'err';
  m.textContent = 'Tesla zwróciła błąd: ' + p.get('error') + ' ' + (p.get('error_description') || '');
}
document.getElementById('b').onclick = async () => {
  try { await navigator.clipboard.writeText(u.value); } catch (e) { u.select(); document.execCommand('copy'); }
  document.getElementById('b').textContent = 'Skopiowano';
};
</script></body></html>`;

const COMMON = {
  "Cache-Control": "no-store",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405, headers: COMMON });
    }
    if (url.pathname === "/.well-known/appspecific/com.tesla.3p.public-key.pem") {
      return new Response(PUBLIC_KEY, {
        headers: { ...COMMON, "Content-Type": "application/x-pem-file", "Cache-Control": "public, max-age=3600" },
      });
    }
    if (url.pathname === "/callback") {
      return new Response(CALLBACK_HTML, {
        headers: {
          ...COMMON,
          "Content-Type": "text/html; charset=utf-8",
          "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'",
        },
      });
    }
    return new Response("Not Found", { status: 404, headers: COMMON });
  },
};
