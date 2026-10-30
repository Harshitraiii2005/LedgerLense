import { DatePipe } from '@angular/common';
import { Component, OnInit, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LiveFeedService, MAX_ITEMS } from '../core/live-feed';

@Component({
  selector: 'app-live-feed',
  imports: [DatePipe, RouterLink],
  template: `
    <div class="page-head">
      <h1>Live flagged transactions</h1>
      <span class="status" [class]="'status ' + feed.status()" data-testid="feed-status">
        {{ feed.status() }}
        @if (feed.status() === 'reconnecting') {
          (attempt {{ feed.attempt() }})
        }
      </span>
    </div>
    <p class="muted">Streamed over a WebSocket from Kafka; newest first, last {{ max }} kept.</p>
    @if (feed.items().length === 0) {
      <p class="card muted">Waiting for flagged transactions…</p>
    }
    <ul class="feed">
      @for (a of feed.items(); track a.txn_id) {
        <li class="card">
          <div class="row">
            <span class="score">{{ a.score }}</span>
            <a [routerLink]="['/customers', a.customer_id]">{{ a.customer_id }}</a>
            <span>{{ a.transaction.merchant }}</span>
            <span class="money">{{ a.transaction.amount }} {{ a.transaction.currency }}</span>
            <span>{{ a.transaction.country }}</span>
            <span class="muted">{{ a.transaction.timestamp | date: 'medium' }}</span>
          </div>
          <div class="reasons">
            @for (r of a.reasons; track r.code) {
              <span class="chip" [title]="r.detail">{{ r.code }}</span>
            }
          </div>
        </li>
      }
    </ul>
  `,
})
export class LiveFeedPage implements OnInit {
  protected readonly feed = inject(LiveFeedService);
  protected readonly max = MAX_ITEMS;

  // The connection lives in a root service, so it survives navigating between pages.
  ngOnInit(): void {
    this.feed.start();
  }
}
