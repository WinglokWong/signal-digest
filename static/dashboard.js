document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form.run-form").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit'], button:not([type])");
      if (!button || button.disabled) return;
      const sending = Boolean(form.querySelector("input[name='send']"));
      button.disabled = true;
      button.textContent = sending ? "正在采集并发送…" : "正在采集…";
      const banner = document.createElement("div");
      banner.className = "sending-banner";
      banner.setAttribute("role", "status");
      banner.textContent = sending
        ? "正在采集最新内容并发送邮件，请勿重复点击…"
        : "正在采集最新内容，请稍候…";
      document.body.appendChild(banner);
    });
  });
});
