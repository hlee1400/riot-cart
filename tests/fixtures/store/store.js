// Fake store helpers: cart lives in localStorage.
function getCart() { return JSON.parse(localStorage.getItem("cart") || "[]"); }
function setCart(c) { localStorage.setItem("cart", JSON.stringify(c)); }
function addLine(title, options, qty, price) {
  const c = getCart();
  const hit = c.find(l => l.title === title && l.options === options);
  if (hit) hit.qty += qty; else c.push({title, options, qty, price});
  setCart(c);
  const a = document.getElementById("added"); if (a) a.hidden = false;
}
function pressable(selector, attr) {
  document.querySelectorAll(selector).forEach(b => b.addEventListener("click", () => {
    if (b.disabled) return;
    document.querySelectorAll(selector).forEach(o => o.setAttribute(attr, "false"));
    b.setAttribute(attr, "true");
  }));
}
function pressed(selector, attr) {
  const b = document.querySelector(selector + "[" + attr + "=true]");
  return b ? b.textContent.trim() : null;
}
function showError(msg) { document.getElementById("error").textContent = msg; }
