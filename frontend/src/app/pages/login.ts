import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';

import { ApiError, ApiService } from '../core/api';
import { AuthService } from '../core/auth';
import { Role } from '../core/models';
import { ErrorBox } from './shared';

/**
 * Dev login: asks the gateway's /dev/token (only registered when DEV_MODE=true) for a JWT.
 * A real deployment would use the bank's identity provider instead.
 */
@Component({
  selector: 'app-login',
  imports: [FormsModule, ErrorBox],
  template: `
    <section class="card narrow">
      <h1>Sign in</h1>
      <form (ngSubmit)="submit()">
        <label>Username <input name="sub" [(ngModel)]="sub" required /></label>
        <label>
          Role
          <select name="role" [(ngModel)]="role">
            <option value="analyst">analyst (read)</option>
            <option value="admin">admin (read + audit)</option>
          </select>
        </label>
        <button type="submit" [disabled]="busy() || !sub">Get dev token</button>
      </form>
      <app-error [error]="error()" />
      <p class="muted">Dev mode only. Tokens are kept in this browser tab.</p>
    </section>
  `,
})
export class LoginPage {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  sub = 'analyst-1';
  role: Role = 'analyst';
  readonly busy = signal(false);
  readonly error = signal<ApiError | null>(null);

  submit(): void {
    this.busy.set(true);
    this.api.devToken(this.sub, this.role).subscribe({
      next: (r) => {
        this.auth.login(r.access_token);
        void this.router.navigate(['/live']);
      },
      error: (e: ApiError) => {
        this.error.set(e);
        this.busy.set(false);
      },
    });
  }
}
