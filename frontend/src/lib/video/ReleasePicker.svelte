<script lang="ts">
	import { t } from '$lib/i18n';
	import { Button, Dialog, Tag, formatBytes, formatNumber, json, plural, request, toasts } from '$lib/kit';

	type Release = {
		channel: string;
		title: string;
		size: number;
		protocol: string;
		seeders: number | null;
		indexer: string;
		guid: string;
		score: number;
		resolution: string;
		source: string;
		video_codec: string;
		subs_hint: boolean;
		audio_codec: string;
		audio_channels: string;
		hdr: string;
		mbps: number | null;
		age_days: number | null;
		payload: number | null;
		declared: string;
		standing: '' | 'held' | 'coming' | 'failed';
		ref: Record<string, unknown>;
	};

	let {
		base,
		title,
		onclose,
		ongrabbed
	}: { base: string; title: string; onclose: () => void; ongrabbed: () => void } = $props();

	let releases = $state<Release[]>([]);
	let loading = $state(true);
	let failed = $state(false);
	let grabbing = $state(false);
	const best = $derived(releases.findIndex((r) => !r.standing));

	async function load() {
		loading = true;
		failed = false;
		const got = await request<Release[]>(`${base}/releases`);
		loading = false;
		if (got) releases = got;
		else failed = true;
	}

	$effect(() => {
		load();
	});

	async function grab(release: Release) {
		if (grabbing) return;
		grabbing = true;
		const started = await request(`${base}/grab`, json(release));
		grabbing = false;
		if (!started) return;
		toasts.success(t('picker.grabStarted', { release: release.title }));
		ongrabbed();
		onclose();
	}

	async function auto() {
		if (grabbing) return;
		grabbing = true;
		const said = await request<{ release: string }>(`${base}/search`, { method: 'POST' }, {
			on: { 404: () => toasts.info(t('picker.empty')) }
		});
		grabbing = false;
		if (!said) return;
		toasts.success(t('picker.grabStarted', { release: said.release }));
		ongrabbed();
		onclose();
	}
</script>

<Dialog title={t('picker.heading')} subtitle={title} size="wide" {onclose}>
	{#snippet actions()}
		<Button onclick={auto} disabled={grabbing}>{t('picker.auto')}</Button>
	{/snippet}
	{#if loading}
		<p class="note">{t('picker.loading')}</p>
	{:else if failed}
		<p class="note failed">{t('picker.searchError')}</p>
	{:else if releases.length === 0}
		<p class="note">{t('picker.empty')}</p>
	{:else}
		<p class="count">{plural(releases.length, 'picker.count.one', 'picker.count.few', 'picker.count.many')}</p>
		<ul>
			{#each releases as r, i (r.title + i)}
				<li>
					<button class="rel" onclick={() => grab(r)} disabled={grabbing || !!r.standing}>
						<Tag>{r.resolution || '—'}</Tag>
						<span class="main">
							<span class="rtitle">{r.title}</span>
							<span class="meta">
								{#if r.standing === 'held'}
									<Tag>{t('picker.held')}</Tag>
								{:else if r.standing === 'coming'}
									<Tag tone="busy">{t('picker.coming')}</Tag>
								{:else if r.standing === 'failed'}
									<Tag tone="err" title={t('picker.failedHint')}>{t('picker.failed')}</Tag>
								{:else if i === best}
									<Tag tone="ok">{t('picker.best')}</Tag>
								{/if}
								<Tag tone={r.protocol === 'torrent' ? 'warn' : 'busy'}>{r.protocol}</Tag>
								{#if r.subs_hint}
									<Tag tone="busy" title={t('picker.subsHint')}>{t('picker.subs')}</Tag>
								{/if}
								{#if r.hdr}<Tag>{r.hdr.toUpperCase()}</Tag>{/if}
								{#if r.source}<span class="dim">{r.source}</span>{/if}
								{#if r.video_codec}<span class="dim">{r.video_codec}</span>{/if}
								{#if r.audio_codec}
									<span class="dim">
										{r.audio_codec}{r.audio_channels ? ` ${r.audio_channels}` : ''}
									</span>
								{/if}
								<span class="dim" title={r.payload ? t('picker.declared') : ''}>
									{r.payload || r.size ? formatBytes(r.payload || r.size) : '—'}
								</span>
								{#if r.declared === 'archive'}
									<Tag tone="err">{t('picker.packed')}</Tag>
								{:else if r.declared === 'unreadable'}
									<Tag tone="err">{t('picker.noVideo')}</Tag>
								{/if}
								{#if r.mbps}
									<span class="dim" title={t('picker.bitrate')}>{formatNumber(r.mbps)} Mbps</span>
								{/if}
								{#if r.protocol === 'torrent'}
									<span class="dim" title={t('picker.seeders', { n: formatNumber(r.seeders ?? 0) })}>
										▲ {formatNumber(r.seeders ?? 0)}
									</span>
								{/if}
								{#if r.indexer}<span class="dim indexer">{r.indexer}</span>{/if}
							</span>
						</span>
						<span class="score" title={t('picker.score')}>{formatNumber(r.score)}</span>
					</button>
				</li>
			{/each}
		</ul>
	{/if}
</Dialog>

<style>
	.note {
		color: var(--muted);
		padding: 1.5rem 0.5rem;
		text-align: center;
	}
	.note.failed {
		color: var(--danger);
	}
	.count {
		color: var(--muted);
		font-size: var(--fs-s);
		margin: 0 0.5rem 0.6rem;
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.rel {
		width: 100%;
		display: flex;
		align-items: center;
		gap: 0.85rem;
		text-align: left;
		font: inherit;
		padding: 0.55rem 0.75rem;
		border-radius: 9px;
		border: 1px solid var(--border);
		background: var(--bg);
		color: var(--text);
		cursor: pointer;
	}
	.rel:hover:not(:disabled) {
		border-color: var(--accent);
		background: var(--surface-2);
	}
	.rel:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.main {
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		min-width: 0;
	}
	.rtitle {
		font-size: var(--fs-m);
		line-height: 1.3;
		overflow-wrap: anywhere;
	}
	.meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--fs-s);
	}
	.dim {
		color: var(--muted);
	}
	.indexer {
		font-style: italic;
	}
	.score {
		flex: none;
		font-size: var(--fs-m);
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--muted);
		min-width: 3rem;
		text-align: right;
	}
</style>
