/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './app/**/*.{ts,tsx}',
    './components/**/*.{ts,tsx}',
    './lib/**/*.{ts,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // ADR-016 §7 디자인 토큰
        support: {
          strong: '#16a34a',   // green-600
          medium: '#ca8a04',   // yellow-600
        },
        citation: {
          direct: '#2563eb',     // blue-600
          synthesis: '#9333ea',  // purple-600
          inference: '#ea580c',  // orange-600
          conflict: '#dc2626',   // red-600
        },
      },
    },
  },
  plugins: [],
};
