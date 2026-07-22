import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'
import { useFiltersStore } from './stores/filters'
import './assets/main.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// Wire the filter store to the router for two-way URL <-> state sync. Must run
// after both plugins are installed and before mount so the initial URL query is
// reflected into the store on first paint.
useFiltersStore().bindToRouter(router)

app.mount('#app')
