// Cloudflare Workers の入口。
// public/ の画面ファイルは Cloudflare が直接配信し、/api/* だけがここに来る。
import { handleApi } from "./api.js";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) return handleApi(request, env);
    return env.ASSETS.fetch(request);
  },
};
