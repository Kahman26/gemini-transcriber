// Прокси для Gemini API на Cloudflare Workers.
// Google видит адрес Cloudflare (Европа), а не ваш, поэтому не срабатывает
// «User location is not supported». Ключ Gemini сюда не вписывается —
// его передаёт скрипт на компьютере в каждом запросе.

export default {
  async fetch(request) {
    const url = new URL(request.url);
    url.protocol = "https:";
    url.hostname = "generativelanguage.googleapis.com";
    url.port = "";

    const upstream = new Request(url.toString(), request);
    for (const h of ["cf-connecting-ip", "cf-ipcountry", "cf-ray", "cf-visitor",
                     "x-forwarded-for", "x-forwarded-proto", "x-real-ip"]) {
      upstream.headers.delete(h);
    }
    return fetch(upstream);
  },
};
