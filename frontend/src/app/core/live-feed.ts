import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { MonoTypeOperatorFunction, Observable, Subscription, repeat, retry, tap, timer } from 'rxjs';
import { webSocket } from 'rxjs/webSocket';

import { AuthService } from './auth';
import { RiskAssessment } from './models';

export type FeedStatus = 'connecting' | 'live' | 'reconnecting' | 'offline';

export const MAX_ITEMS = 200;

/** 1s, 2s, 4s ... capped at 30s. `attempt` starts at 1. */
export function backoffMs(attempt: number, baseMs = 1000, maxMs = 30_000): number {
  return Math.min(baseMs * 2 ** (attempt - 1), maxMs);
}

/**
 * Resubscribe forever with exponential backoff, on errors (network drop, rejected handshake)
 * AND on clean completion (server closed the socket, e.g. a gateway restart).
 * resetOnSuccess: once a message arrives, the next failure starts again at 1s.
 */
export function withBackoff<T>(onRetry: (attempt: number) => void): MonoTypeOperatorFunction<T> {
  let attempt = 0;
  const wait = () => {
    attempt += 1;
    onRetry(attempt);
    return timer(backoffMs(attempt));
  };
  return (source: Observable<T>) =>
    source.pipe(
      tap(() => (attempt = 0)),
      retry({ delay: wait }),
      repeat({ delay: wait }),
    );
}

/**
 * Optional `<meta name="ledgerlens-ws-base" content="wss://gateway.example">` override, for hosts
 * that can't proxy WebSockets (Vercel rewrites). Unset/placeholder = same-origin /api proxy.
 */
export function wsBaseOverride(doc: Document = document): string | null {
  const v = doc.querySelector<HTMLMetaElement>('meta[name="ledgerlens-ws-base"]')?.content ?? '';
  return v.startsWith('ws') ? v.replace(/\/$/, '') : null;
}

export function feedUrl(token: string, loc: Location = location, base: string | null = wsBaseOverride()): string {
  const q = `/v2/ws/flagged?token=${encodeURIComponent(token)}`;
  if (base) return `${base}${q}`;
  const scheme = loc.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${loc.host}/api${q}`;
}

@Injectable({ providedIn: 'root' })
export class LiveFeedService {
  private readonly auth = inject(AuthService);
  readonly status = signal<FeedStatus>('offline');
  readonly attempt = signal(0);
  readonly items = signal<RiskAssessment[]>([]);
  private sub: Subscription | null = null;

  constructor() {
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  start(): void {
    const token = this.auth.token();
    if (this.sub || !token) return;
    this.status.set('connecting');
    const socket$ = webSocket<RiskAssessment>({
      url: feedUrl(token),
      openObserver: { next: () => (this.status.set('live'), this.attempt.set(0)) },
    });
    this.sub = socket$
      .pipe(
        withBackoff((n) => {
          this.status.set('reconnecting');
          this.attempt.set(n);
        }),
      )
      .subscribe((msg) => this.items.update((xs) => [msg, ...xs].slice(0, MAX_ITEMS)));
  }

  stop(): void {
    this.sub?.unsubscribe();
    this.sub = null;
    this.status.set('offline');
  }
}
