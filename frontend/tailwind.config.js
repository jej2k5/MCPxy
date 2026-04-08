/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        surface: {
          950: "#0b0d12",
          900: "#11141c",
          800: "#161a24",
          700: "#1e2330",
          600: "#2a3142",
          500: "#3a4257",
        },
        accent: {
          500: "#5b8cff",
          400: "#7da3ff",
        },
        ok: "#34d399",
        warn: "#fbbf24",
        err: "#f87171",
        denied: "#c084fc",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
