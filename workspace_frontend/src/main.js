import { createApp } from 'vue'
import PrimeVue from 'primevue/config'
import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'
import App from './App.vue'
import './styles.css'
import { loadLabelRuntime } from './label-runtime.js'

loadLabelRuntime()

// FU73: PrimeVue 4 dipasang sekali untuk seluruh app, tapi komponen hanya
// diimpor lokal di Dashboard.vue — halaman lain tidak berubah.
// Ramp primary biru app: 500 = --brand #3368a0, 600/700 ≈ --brand-strong.
const workspacePreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '#f2f6fa',
      100: '#e3eef5',
      200: '#c2d8e9',
      300: '#9bbdd8',
      400: '#6b96bd',
      500: '#3368a0',
      600: '#2a5585',
      700: '#23466f',
      800: '#1d3a5b',
      900: '#182e48',
      950: '#0f1e30'
    }
  }
})

createApp(App)
  .use(PrimeVue, {
    theme: {
      preset: workspacePreset,
      options: {
        // tema app terang saja — selector false mematikan dark mode PrimeVue
        darkModeSelector: false
      }
    }
  })
  .mount('#app')

// PWA: /sw.js has root scope; registration only exists on secure contexts, and a
// failure here must never break the workspace itself.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
