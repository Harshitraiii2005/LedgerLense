import { Component, input } from '@angular/core';

import { ApiError } from '../core/api';

/** Renders the gateway's error schema; request_id lets support find the request in the logs. */
@Component({
  selector: 'app-error',
  template: `
    @if (error(); as e) {
      <div class="error" role="alert">
        <b>{{ e.code }}</b> {{ e.message }}
        @if (e.request_id) {
          <div class="muted">request_id: <code>{{ e.request_id }}</code></div>
        }
      </div>
    }
  `,
})
export class ErrorBox {
  readonly error = input<ApiError | null>(null);
}
