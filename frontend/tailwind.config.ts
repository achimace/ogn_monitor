import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Tower-optimized colors (high contrast, dark mode)
        tower: {
          bg: '#0a0e17',
          surface: '#111827',
          border: '#1f2937',
          fly: '#5697d6',
          ldg: '#0cb300',
          alarm: '#dc3545',
          emergency: '#ff0040',
          towing: '#f59e0b',
          qdr: '#06b6d4',      // Cyan for bearing
          altitude: '#eab308', // Yellow for altitude
          distance: '#06b6d4', // Cyan for distance
        },
      },
      fontSize: {
        // Large tower display sizes (readable from 3m)
        'tower-xl': ['2rem', { lineHeight: '2.5rem' }],
        'tower-2xl': ['2.5rem', { lineHeight: '3rem' }],
        'tower-3xl': ['3rem', { lineHeight: '3.5rem' }],
      },
    },
  },
  plugins: [],
} satisfies Config
