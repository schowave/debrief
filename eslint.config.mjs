// Lints the inline script of the review page and i18n.js: `mise run lint`
import js from "@eslint/js";
import html from "eslint-plugin-html";
import globals from "globals";

export default [{
  files: ["static/**/*.html", "static/**/*.js"],
  plugins: { html },
  languageOptions: { ecmaVersion: "latest", sourceType: "script", globals: { ...globals.browser, EN: "readonly", DE: "readonly" } },
  rules: {
    ...js.configs.recommended.rules,  // e.g. `!x === 1` (no-constant-binary-expression), unused variables, empty blocks
    "no-unused-vars": ["error", { ignoreRestSiblings: true }],  // `const { drop, ...keep } = x`
    "no-restricted-syntax": ["error", {  // the standard rules do not catch this one
      selector: "BinaryExpression[operator=/^[!=]==?$/][left.type='UnaryExpression'][left.operator='!'][right.type='Literal']",
      message: "`!x === y` negates x first. Write `x !== y` or `!(x === y)`.",
    }],
    "no-shadow": "error",  // a local `t` hid the translate function t() and broke a toast
    "no-undef": "error",
  },
}, {
  files: ["static/i18n.js"],  // defines EN and DE for the pages
  languageOptions: { globals: { EN: "off", DE: "off" } },
}];
