// A library job that runs on the server and is watched from here: a scan, an
// enrichment pass. Asked once when a page opens and polled for as long as it is
// running — whoever started it, and whenever — so a page opened in the middle
// of a scan follows it to the end instead of showing a bar that never moves.

import { untrack } from 'svelte';
import { request, toasts } from '$lib/kit';
import { t, type MessageKey } from '$lib/i18n';

type Running = { running: boolean };

export class Job<S extends Running> {
	state = $state<S | null>(null);
	#url: string;
	#timer: ReturnType<typeof setInterval> | undefined;
	#said: { started: MessageKey; already: MessageKey };
	#onfinish?: (state: S) => void;
	#ontick?: (state: S) => void;

	constructor(
		url: string,
		said: { started: MessageKey; already: MessageKey },
		on: { finish?: (state: S) => void; tick?: (state: S) => void } = {}
	) {
		this.#url = url;
		this.#said = said;
		this.#onfinish = on.finish;
		this.#ontick = on.tick;
	}

	get running(): boolean {
		return this.state?.running ?? false;
	}

	/** Called from effects, so what it reads before asking is read untracked: a
	 *  page that re-ran its effect whenever the job moved would ask again for
	 *  everything else it loads, on every poll. */
	async load() {
		const was = untrack(() => this.running);
		const now = await request<S>(this.#url);
		if (!now) {
			this.stop();
			return;
		}
		this.state = now;
		this.#ontick?.(now);
		if (now.running) {
			this.#timer ??= setInterval(() => this.load(), 2000);
		} else {
			this.stop();
			if (was) this.#onfinish?.(now);
		}
	}

	async start() {
		const started = await request(
			this.#url,
			{ method: 'POST' },
			{
				on: {
					409: () => {
						toasts.info(t(this.#said.already));
						this.load();
					}
				}
			}
		);
		if (!started) return;
		toasts.info(t(this.#said.started));
		await this.load();
	}

	stop() {
		clearInterval(this.#timer);
		this.#timer = undefined;
	}
}
