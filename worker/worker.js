export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).text();
    const headers = {};
    for (const [k, v] of message.headers) {
      headers[k] = v;
    }
    const payload = {
      from: message.from,
      to: message.to,
      headers: headers,
      raw: raw
    };
    try {
      const res = await fetch("https://mail.yourdomain.com/incoming-email", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Webhook-Secret": env.WEBHOOK_SECRET
        },
        body: JSON.stringify(payload)
      });
      if (!res.ok && res.status >= 500) {
        message.setReject("backend unavailable");
      }
    } catch (e) {
      message.setReject("backend unreachable");
    }
  }
};
