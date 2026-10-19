// Lints the inline script of the review page and i18n.js: `mise run lint`
import html from "eslint-plugin-html";
import globals from "globals";

export default [{
  files: ["static/**/*.html", "static/**/*.js"],
  plugins: { html },
  languageOptions: { ecmaVersion: "latest", sourceType: "script", globals: { ...globals.browser, EN: "readonly", DE: "readonly" } },
  rules: {
    "no-shadow": "error",  // a local `t` hid the translate function t() and broke a toast
    "no-undef": "error",
  },
}];
