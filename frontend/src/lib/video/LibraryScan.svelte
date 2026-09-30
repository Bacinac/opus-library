<script lang="ts">
	import { t, type MessageKey } from '$lib/i18n';
	import { Button, Heading, Progress, Stats, Tag, formatNumber, json, request, toasts, type Stat } from '$lib/kit';
	import { Job } from '$lib/core/job.svelte';
	import ResolveMatch from '$lib/video/ResolveMatch.svelte';

	type Result = { file: string; path: string; outcome: string; guessed?: string; series?: string };
	type ScanState = {
		running: boolean;
		phase: string;
		error: string;
		total: number;
		processed: number;
		movies: number;
		series: number;
		episodes: number;
		unmatched: number;
		green: number;
		current: string;
		results: Result[];
		started_at: string | null;
		finished_at: string | null;
	};

	type Held = { complete: number; waiting: number };
	type Totals = {
		movies: Held;
		series: Held;
		movie_count: number;
		series_count: number;
		episodes: number;
	};

	const CLEAN = new Set(['movie', 'episode', 'series', 'already_adopted']);
	const SHOWN = 400;

	let { type }: { type: 'movies' | 'series' } = $props();

	let stats = $state<Totals | null>(null);
	let resolving = $state<Result | null>(null);

	async function loadStats() {
		stats = (await request<Totals>('/api/video/library/stats')) ?? stats;
	}

	const scan = new Job<ScanState>(
		'/api/video/library/scan',
		{ started: 'video.library.started', already: 'video.library.alreadyRunning' },
		{ finish: () => loadStats() }
	);

	$effect(() => {
		scan.load();
		loadStats();
		return () => scan.stop();
	});

	function removeReview(r: Result) {
		if (scan.state) scan.state.results = scan.state.results.filter((x) => x.path !== r.path);
		loadStats();
	}

	async function ignore(r: Result) {
		if (!(await request('/api/video/library/ignore', json({ path: r.path })))) return;
		toasts.success(t('review.ignored'));
		removeReview(r);
	}

	let tiles = $derived<Stat[]>(
		!stats
			? []
			: type === 'movies'
				? [
						{ label: t('stats.movies'), value: stats.movie_count },
						{ label: t('stats.complete'), value: stats.movies.complete, tone: 'ok' },
						{ label: t('stats.waiting'), value: stats.movies.waiting, tone: 'warn' }
					]
				: [
						{ label: t('stats.series'), value: stats.series_count },
						{ label: t('stats.episodes'), value: stats.episodes },
						{ label: t('stats.complete'), value: stats.series.complete, tone: 'ok' },
						{ label: t('stats.waiting'), value: stats.series.waiting, tone: 'warn' }
					]
	);

	let now = $derived(scan.state);
	let attention = $derived((now?.results ?? []).filter((r) => !CLEAN.has(r.outcome)));
</script>

<Stats stats={tiles} />

<div class="action-row">
	<Button tone="accent" onclick={() => scan.start()} disabled={scan.running}>
		{scan.running ? t('action.scanning') : t('action.scan')}
	</Button>
</div>

{#if now}
	{#if now.phase === 'error' || now.phase === 'refused'}
		<p class="failed">{t('video.library.error', { detail: now.error })}</p>
	{/if}

	{#if now.running}
		<Progress value={now.total ? now.processed / now.total : 0} />
		<div class="progress-row">
			<span class="muted">
				{t(`video.library.phase.${now.phase}` as MessageKey)} · {formatNumber(now.processed)}/{formatNumber(now.total)}
			</span>
			{#if now.current}<span class="muted current">{now.current}</span>{/if}
		</div>
	{:else if now.processed > 0}
		<p class="muted lastscan">
			{t('video.library.lastScan', {
				total: formatNumber(now.total),
				added: formatNumber(now.movies + now.series + now.episodes),
				unmatched: formatNumber(now.unmatched)
			})}
		</p>
	{/if}

	{#if attention.length > 0}
		<Heading label={t('video.library.attention')} count={attention.length} />
		<ul class="attention">
			{#each attention.slice(0, SHOWN) as r (r.path)}
				<li>
					<Tag tone={r.outcome === 'error' || r.outcome === 'tmdb_error' ? 'err' : 'warn'}>
						{t(`video.library.outcome.${r.outcome}` as MessageKey)}
					</Tag>
					<span class="file">{r.file}</span>
					{#if r.guessed}<span class="muted">→ {r.guessed}</span>{/if}
					<span class="acts">
						<Button size="small" tone="accent" onclick={() => (resolving = r)}>{t('review.resolve')}</Button>
						<Button size="small" onclick={() => ignore(r)}>{t('review.ignore')}</Button>
					</span>
				</li>
			{/each}
		</ul>
		{#if attention.length > SHOWN}
			<p class="muted">{t('video.library.more', { n: formatNumber(attention.length - SHOWN) })}</p>
		{/if}
	{/if}
{/if}

{#if resolving}
	<ResolveMatch
		path={resolving.path}
		filename={resolving.file}
		guessed={resolving.guessed ?? resolving.series}
		onclose={() => (resolving = null)}
		onresolved={() => resolving && removeReview(resolving)}
	/>
{/if}

<style>
	.failed {
		color: var(--danger);
	}
	.progress-row {
		display: flex;
		justify-content: space-between;
		gap: 1rem;
		margin: 0.4rem 0 1rem;
		font-size: var(--fs-m);
	}
	.current {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 55%;
	}
	.attention {
		list-style: none;
		padding: 0;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.attention li {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
		padding: 0.4rem 0.7rem;
		font-size: var(--fs-m);
	}
	.file {
		overflow-wrap: anywhere;
	}
	.acts {
		margin-left: auto;
		display: flex;
		gap: 0.4rem;
		flex: none;
	}
</style>
