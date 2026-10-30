import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';

import { Role } from './models';

const STORAGE_KEY = 'ledgerlens.token';

interface Claims {
  sub: string;
  role: Role;
  exp: number;
}

/** Reads the JWT payload for display/routing only. The gateway verifies the signature on every call. */
export function decodeClaims(token: string): Claims | null {
  try {
    const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(part)) as Claims;
  } catch {
    return null;
  }
}

function readStored(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY); // per-tab: closing the tab logs out
  } catch {
    return null;
  }
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  readonly token = signal<string | null>(readStored());
  readonly claims = computed(() => {
    const t = this.token();
    const c = t ? decodeClaims(t) : null;
    return c && c.exp * 1000 > Date.now() ? c : null;
  });
  readonly role = computed(() => this.claims()?.role ?? null);
  readonly isAdmin = computed(() => this.role() === 'admin');

  login(token: string): void {
    this.token.set(token);
    try {
      sessionStorage.setItem(STORAGE_KEY, token);
    } catch {
      /* storage blocked: token stays in memory only */
    }
  }

  logout(): void {
    this.token.set(null);
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }
}

export function newRequestId(): string {
  // crypto.randomUUID only exists in secure contexts (https or localhost).
  return globalThis.crypto?.randomUUID?.() ?? Math.random().toString(16).slice(2) + Date.now().toString(16);
}

/** Adds the Bearer token and an X-Request-ID; a 401 means the session is over. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const token = auth.token();
  const headers: Record<string, string> = { 'X-Request-ID': newRequestId() };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  return next(req.clone({ setHeaders: headers })).pipe(
    catchError((err: unknown) => {
      if (err instanceof HttpErrorResponse && err.status === 401 && token) {
        auth.logout();
        void router.navigate(['/login']);
      }
      return throwError(() => err);
    }),
  );
};

export const authGuard: CanActivateFn = () =>
  inject(AuthService).claims() ? true : inject(Router).createUrlTree(['/login']);

/** UX only: the gateway enforces admin on /v2/audit regardless (403). */
export const adminGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (!auth.claims()) return router.createUrlTree(['/login']);
  return auth.isAdmin() ? true : router.createUrlTree(['/forbidden']);
};
