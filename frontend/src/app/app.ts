import { Component, inject } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { AuthService } from './core/auth';
import { LiveFeedService } from './core/live-feed';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <header class="topbar">
      <a class="brand" routerLink="/">LedgerLens</a>
      @if (auth.claims(); as c) {
        <nav>
          <a routerLink="/live" routerLinkActive="active">Live feed</a>
          <a routerLink="/flagged" routerLinkActive="active">Flagged</a>
          <a routerLink="/agent" routerLinkActive="active">Ask the data</a>
          @if (auth.isAdmin()) {
            <a routerLink="/audit" routerLinkActive="active">Audit log</a>
          }
        </nav>
        <span class="who">{{ c.sub }} · <b>{{ c.role }}</b></span>
        <button class="link" (click)="logout()">Log out</button>
      }
    </header>
    <main><router-outlet /></main>
  `,
})
export class App {
  protected readonly auth = inject(AuthService);
  private readonly feed = inject(LiveFeedService);
  private readonly router = inject(Router);

  logout(): void {
    this.feed.stop();
    this.auth.logout();
    void this.router.navigate(['/login']);
  }
}
