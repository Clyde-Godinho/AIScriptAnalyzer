import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ['ui-monospace', "SFMono-Regular", "Menlo", "Consolas", '"Liberation Mono"', "monospace"],
      },
      colors: {
        emerald: {
          100: "#d1fae5",
          200: "#a7f3d0",
          300: "#6ee7b7",
          500: "#10b981",
          700: "#047857",
          800: "#065f46",
          950: "#022c22",
        },
        amber: {
          100: "#fef3c7",
          200: "#fde68a",
          400: "#fbbf24",
          600: "#d97706",
          800: "#92400e",
          950: "#451a03",
        },
        rose: {
          100: "#ffe4e6",
          200: "#fecdd3",
          500: "#f43f5e",
          700: "#be123c",
          800: "#9f1239",
          950: "#4c0519",
        },
        slate: {
          300: "#cbd5e1",
          400: "#94a3b8",
          500: "#64748b",
          600: "#475569",
          800: "#1e293b",
          900: "#0f172a",
          950: "#020617",
        },
        sky: {
          100: "#e0f2fe",
          300: "#7dd3fc",
          400: "#0ea5e9",
          500: "#0284c7",
          600: "#0369a1",
          900: "#0c4a6e",
        },
        violet: {
          200: "#ddd6fe",
          800: "#5b21b6",
          900: "#4c1d95",
        },
      },
      keyframes: {
        "fade-in": {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "slide-in": {
          "0%": { transform: "translateX(100%)" },
          "100%": { transform: "translateX(0)" },
        },
      },
      animation: {
        "fade-in": "fade-in .18s ease-out both",
        "slide-in": "slide-in .22s ease-out both",
      },
    },
  },
  plugins: [],
};

export default config;
