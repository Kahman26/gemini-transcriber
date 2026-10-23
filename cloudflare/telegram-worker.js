// Прокси для Telegram Bot API на Cloudflare Workers.
//
// Сервер транскрибера стоит в России, где Telegram замедляют и блокируют.
// Сервер шлёт запросы сюда, а Worker — в api.telegram.org с адреса Cloudflare.
//
// Чтобы прокси не стал бесплатным для всех подряд, он пускает только запросы
// с заголовком X-Proxy-Secret, совпадающим с секретом PROXY_SECRET
// (Settings → Variables and Secrets в панели Worker'а), и только к Bot API:
// методы бота и скачивание присланных ему файлов.
// Токен бота сюда не вписывается — его передаёт сервер в адресе запроса.

const BOT_METHOD = /^\/bot\d+:[A-Za-z0-9_-]+\/[A-Za-z]+$/;
const BOT_FILE = /^\/file\/bot\d+:[A-Za-z0-9_-]+\/[A-Za-z0-9_.\/-]+$/;

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function allowed(request, url) {
  if (BOT_METHOD.test(url.pathname)) return ["GET", "POST"].includes(request.method);
  if (BOT_FILE.test(url.pathname)) return request.method === "GET" && !url.pathname.includes("..");
  return false;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.PROXY_SECRET || !safeEqual(request.headers.get("X-Proxy-Secret") || "", env.PROXY_SECRET)
        || !allowed(request, url)) {
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
