import { currentUser, login, register, toast } from "./api.js";

const $ = (id) => document.getElementById(id);
const forms = { login: $("login-form"), register: $("register-form") };

function show(which) {
  for (const [name, form] of Object.entries(forms)) form.hidden = name !== which;
  $("tab-login").classList.toggle("active", which === "login");
  $("tab-register").classList.toggle("active", which === "register");
  $("tab-login").setAttribute("aria-selected", which === "login");
  $("tab-register").setAttribute("aria-selected", which === "register");
  $("form-error").hidden = true;
}

function fail(err) {
  const box = $("form-error");
  const fields = (err.details || []).map((d) => `${d.field}: ${d.message}`).join("; ");
  box.textContent = fields || err.message || "Something went wrong";
  box.hidden = false;
}

async function submit(form, action) {
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  $("form-error").hidden = true;
  try { await action(); location.replace("dashboard.html"); }
  catch (err) { fail(err); }
  finally { button.disabled = false; }
}

$("tab-login").addEventListener("click", () => show("login"));
$("tab-register").addEventListener("click", () => show("register"));
forms.login.addEventListener("submit", (e) => {
  e.preventDefault();
  submit(forms.login, () => login($("login-email").value.trim(), $("login-password").value));
});
forms.register.addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("register-password").value !== $("register-confirm").value) return fail({ message: "Passwords do not match" });
  submit(forms.register, () => register($("register-name").value.trim(), $("register-email").value.trim(), $("register-password").value));
});

currentUser().then((user) => { if (user) location.replace("dashboard.html"); })
  .catch(() => toast("Cannot reach the PDF Master Pro server. Is the backend running?"));
