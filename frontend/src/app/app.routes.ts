import { Routes } from '@angular/router';

import { adminGuard, authGuard } from './core/auth';

// Pages are lazy-loaded so the initial bundle only carries the shell + login.
export const routes: Routes = [
  { path: 'login', loadComponent: () => import('./pages/login').then((m) => m.LoginPage) },
  { path: 'forbidden', loadComponent: () => import('./pages/forbidden').then((m) => m.ForbiddenPage) },
  {
    path: '',
    canActivate: [authGuard],
    children: [
      { path: '', pathMatch: 'full', redirectTo: 'live' },
      { path: 'live', loadComponent: () => import('./pages/live-feed').then((m) => m.LiveFeedPage) },
      { path: 'flagged', loadComponent: () => import('./pages/flagged').then((m) => m.FlaggedPage) },
      { path: 'customers/:id', loadComponent: () => import('./pages/customer').then((m) => m.CustomerPage) },
      { path: 'agent', loadComponent: () => import('./pages/agent-chat').then((m) => m.AgentChatPage) },
      {
        path: 'audit',
        canActivate: [adminGuard],
        loadComponent: () => import('./pages/audit').then((m) => m.AuditPage),
      },
    ],
  },
  { path: '**', redirectTo: 'live' },
];
