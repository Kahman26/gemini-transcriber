// Прокси для Telegram Bot API на Cloudflare Workers.
//
// Сервер транскрибера стоит в России, где Telegram замедляют и блокируют.
// Сервер шлёт запросы сюда, а Worker — в api.telegram.org с адреса Cloudflare.
//
// Чтобы прокси не стал бесплатным для всех подряд, он пускает только запросы
// с заголовком X-Proxy-Secret, совпадающим с секретом PROXY_SECRET
// (Settings → Variables and Secrets в панели Worker'а), и только к Bot API.
// Токен бота сюда не вписывается — его передаёт сервер в адресе запроса.

const ALLOWED_PATH = /^\/bot\d+:[A-Za-z0-9_-]+\/[A-Za-z]+$/;

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.PROXY_SECRET || !safeEqual(request.headers.get("X-Proxy-Secret") || "", env.PROXY_SECRET)
        || !ALLOWED_PATH.test(url.pathname) || !["GET", "POST"].includes(request.method)) {
      return new Response("Not found", { status: 404 });
    }

    url.protocol = "https:";
    url.hostname = "api.telegram.org";
    url.port = "";

    const upstream = new Request(url.toString(), request);
    for (const h of ["x-proxy-secret", "cf-connecting-ip", "cf-ipcountry", "cf-ray", "cf-visitor",
                     "x-forwarded-for", "x-forwarded-proto", "x-real-ip"]) {
      upstream.headers.delete(h);
    }
    return fetch(upstream);
  },
};
