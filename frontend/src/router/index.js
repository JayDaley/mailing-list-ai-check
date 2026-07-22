// App routes. The redesign collapses the old five-route app into a single
// dashboard screen at '/'. The drawer is expressed as a route param on the SAME
// Dashboard component so it is deep-linkable and back/forward steps through it.
//
//   /             Dashboard
//   /messages/:id Dashboard  (drawer open on that message)
//
// Legacy paths redirect to '/'. The filters store's URL sync pushes to the
// current path, so it keeps working on both routes unchanged.

import { createRouter, createWebHistory } from 'vue-router'

import Dashboard from '../views/Dashboard.vue'

const routes = [
  { path: '/', name: 'dashboard', component: Dashboard },
  { path: '/messages/:id', name: 'message', component: Dashboard },
  // Legacy routes collapse into the single screen.
  { path: '/messages', redirect: '/' },
  { path: '/persons', redirect: '/' },
  { path: '/lists', redirect: '/' },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
