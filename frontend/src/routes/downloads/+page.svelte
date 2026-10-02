<script lang="ts">
	// One queue for the whole library.
	//
	// This is the page the merge earns outright: once both halves acquire through
	// OPUS · Downloads, a record and a film in flight are the same fact, and
	// nobody should have to look in two places for it.
	//
	// What is NOT merged is the state word. Music settles into complete or
	// rejected; video can sit in waiting_subtitles, which means nothing to an
	// album. Each row is read in its own half's vocabulary, and acted on through
	// its own half's endpoints.

	import { t, type MessageKey } from '$lib/i18n';
	import {
		ArmedButton,
		PageActions,
		Progress,
		Tag,
		formatDateTime,
		formatNumber,
		request,
		toasts
	} from '$lib/kit';
	import type { Kind } from '$lib/opus';
	import type { MediaType, QueueRow } from '$lib/core/types';

	let rows = $state<QueueRow[]>([]);
	let loaded = $state(false);
	let busy = $state<string | null>(null);
	let clearing = $state(false);

	// still in flight, so not a record yet — an import among them owns its files
	// and cannot be interrupted, which is why it offers no action of its own
	const RUNNING = ['queued', 'downloading', 'importing', 'downloaded'];
	const STOPPABLE = ['queued', 'downloading'];

	const KIND: Record<MediaType, Kind | undefined> = {
		music: 'music',
		movies: 'film',
		series: 'series',
		video: undefined,
		photos: 'photos'
	};

	const key = (r: QueueRow) => `${r.domain}-${r.id}`;
	const finishedMusic = $derived(
		rows.filter((r) => r.domain === 'music' && !RUNNING.includes(r.state))
	);

	async function load() {
		const got = await request<QueueRow[]>('/api/downloads');
		if (!got) return;
		rows = got;
		loaded = true;
	}

	async function act(r: QueueRow, what: 'cancel' | 'delete') {
		busy = key(r);
		const base = `/api/${r.domain}/downloads/${r.id}`;
		const done =
			what === 'cancel' && r.domain === 'music'
				? await request(`${base}/cancel`, { method: 'POST' })
				: await request(base, { method: 'DELETE' });
		busy = null;
		if (done) await load();
	}

	async function clearFinished() {
		clearing = true;
		const data = await request<{ deleted: number }>('/api/music/downloads/clear', { method: 'POST' });
		clearing = false;
		if (!data) return;
		toasts.success(t('downloads.cleared', { n: formatNumber(data.deleted) }));
		await load();
	}

	function label(r: QueueRow): string {
		return r.domain === 'music'
			? t(`music.status.${r.state}` as MessageKey)
			: t(`video.queue.${r.state}` as MessageKey);
	}

	function origin(r: QueueRow): string {
		return r.wanted.includes(r.release) ? r.channel : `${r.channel} · ${r.release}`;
	}

	$effect(() => {
		load();
		const interval = setInterval(load, 5000);
		return () => clearInterval(interval);
	});
</script>

{#if finishedMusic.length > 0}
	<PageActions>
		<ArmedButton tone="quiet" disabled={clearing} onconfirm={clearFinished}>
			{t('downloads.clear')}
		</ArmedButton>
	</PageActions>
{/if}

{#if loaded && rows.length === 0}
	<p class="muted">{t('downloads.empty')}</p>
{/if}

{#if rows.length > 0}
	<table>
		<thead>
			<tr>
				<th>{t('downloads.wanted')}</th>
				<th>{t('downloads.state')}</th>
				<th class="time">{t('downloads.time')}</th>
				<th></th>
			</tr>
		</thead>
		<tbody>
			{#each rows as r (key(r))}
				<tr>
					<td class="what">
						<Tag tone="quiet" kind={KIND[r.type]}>{t(`type.${r.type}`)}</Tag>
						{r.wanted}
						<span class="release muted">
							{origin(r)}
						</span>
					</td>
					<td class={`status-${r.state}`}>
						<span class="state">
							{label(r)}
							{#if r.progress != null && RUNNING.includes(r.state)}
								<Progress inline share value={r.progress} />
							{/if}
							{#if r.detail}<span class="note" title={r.detail}>ⓘ</span>{/if}
						</span>
					</td>
					<td class="time">{formatDateTime(r.created_at)}</td>
					<td class="actions">
						{#if STOPPABLE.includes(r.state)}
							<ArmedButton size="small" disabled={busy === key(r)} onconfirm={() => act(r, 'cancel')}>
								{t('downloads.cancel')}
							</ArmedButton>
						{:else if !RUNNING.includes(r.state)}
							<ArmedButton size="small" disabled={busy === key(r)} onconfirm={() => act(r, 'delete')}>
								{t('common.delete')}
							</ArmedButton>
						{/if}
					</td>
				</tr>
				{#if r.files.length > 0}
					<tr class="files">
						<td colspan="4">
							<ul>
								{#each r.files as f (f.name)}
									<li>
										{f.name}
										<span class="muted">— {t(`music.status.${f.state}` as MessageKey)}</span>
									</li>
								{/each}
							</ul>
						</td>
					</tr>
				{/if}
			{/each}
		</tbody>
	</table>
{/if}

<style>
	/* a release name is one unbroken token of dots: only "anywhere" lets it
	   shrink the column, break-word still sizes the table to its full length */
	.what {
		width: 100%;
		white-space: normal;
	}
	.release {
		display: block;
		font-size: var(--fs-s);
		overflow-wrap: anywhere;
	}
	@media (max-width: 620px) {
		.time {
			display: none;
		}
	}
	.state {
		display: inline-flex;
		align-items: center;
		gap: 0.5rem;
	}
	.actions {
		text-align: right;
	}
	.note {
		cursor: help;
		color: var(--muted);
	}
	.files td {
		padding-top: 0;
		white-space: normal;
	}
	.files ul {
		margin: 0;
		padding-left: 1.1rem;
		font-size: var(--fs-m);
	}
</style>
