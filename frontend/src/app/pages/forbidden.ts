import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

@Component({
  selector: 'app-forbidden',
  imports: [RouterLink],
  template: `
    <section class="card narrow">
      <h1>403 · Forbidden</h1>
      <p>This page needs the <b>admin</b> role.</p>
      <a routerLink="/live">Back to the live feed</a>
    </section>
  `,
})
export class ForbiddenPage {}
