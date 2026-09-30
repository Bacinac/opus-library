<script lang="ts">
	// The library page shell: it loads the artists, the unmatched folders and
	// the two job states, keeps them fresh while a job runs, and owns the three
	// buttons that start work. The wall, the banners, the folder list and the
	// report only draw what it holds.

	import { t } from '$lib/i18n';
	import { Button, Stats, formatNumber, request, toasts, type Stat } from '$lib/kit';
	import { Job } from '$lib/core/job.svelte';
	import ArtistGrid from '$lib/music/library/ArtistGrid.svelte';
	import ScanProgress from '$lib/music/library/ScanProgress.svelte';
	import ScanReport from '$lib/music/library/ScanReport.svelte';
	import UnmatchedFolders from '$lib/music/library/UnmatchedFolders.svelte';
	import type {
		ArtistSummary,
		EnrichState,
		MusicStats,
		ScanState,
		UnmatchedFolder
	} from '$lib/music/types';

	let artists = $state<ArtistSummary[]>([]);
	let loaded = $state(false);
	let unmatched = $state<UnmatchedFolder[]>([]);
	let totals = $state<MusicStats | null>(null);
	let scanProgressAt = $state(0);
	let scanFingerprint = '';

	async function loadArtists() {
		const got = await request<ArtistSummary[]>('/api/music/artists');
		if (!got) return;
		artists = got;
		loaded = true;
	}

	async function loadTotals() {
		totals = (await request<MusicStats>('/api/music/library/stats')) ?? totals;
	}

	async function loadUnmatched() {
		unmatched = (await request<UnmatchedFolder[]>('/api/music/library/unmatched')) ?? unmatched;
	}

	async function refreshAfterRematch() {
		await Promise.all([loadArtists(), loadTotals(), loadUnmatched(), scan.load()]);
	}

	const enrich = new Job<EnrichState>(
		'/api/music/library/enrich',
		{ started: 'libenrich.started', already: 'libenrich.already' },
		{
			finish: (done) => {
				toasts.success(
					t('libenrich.done', {
						processed: formatNumber(done.processed),
						failed: formatNumber(done.failed.length)
					})
				);
				loadArtists();
				loadTotals();
			}
		}
	);

	// an import scan enriches what it adopts, so while it runs the enrichment it
	// starts is watched too
	const scan = new Job<ScanState>(
		'/api/music/library/scan',
		{ started: 'libimport.started', already: 'libimport.already' },
		{
			tick: (now) => {
				// Only changed phase/counters/current folder or a new verdict counts
				// as progress. A poll while a catalog request is hung must not make
				// the screen appear healthy.
				const newest = now.results.at(-1);
				const fingerprint = [
					now.phase,
					now.processed,
					now.total,
					now.deep_processed,
					now.deep_total,
					now.current,
					now.deep_current,
					now.results.length,
					newest?.folder,
					newest?.outcome
				].join('|');
				if (fingerprint !== scanFingerprint) {
					scanFingerprint = fingerprint;
					scanProgressAt = Date.now();
				}
				if (now.running && !enrich.running) enrich.load();
			},
			finish: (done) => {
				toasts.success(
					t('libimport.summary', {
						matched: formatNumber(done.matched),
						tracks: formatNumber(done.adopted_tracks),
						problems: formatNumber(done.results.filter((r) => r.outcome !== 'adopted').length)
					})
				);
				loadArtists();
				loadUnmatched();
				loadTotals();
				enrich.load();
			}
		}
	);

	let stats = $derived<Stat[]>(
		totals
			? [
					{ label: t('stats.artists'), value: totals.artists },
					{ label: t('stats.albums'), value: totals.albums },
					{ label: t('stats.tracks'), value: totals.tracks },
					{ label: t('stats.complete'), value: totals.complete, tone: 'ok' },
					{
						label: t('stats.doubtful'),
						value: totals.doubtful,
						tone: 'warn',
						href: '/incomplete'
					}
				]
			: []
	);

	$effect(() => {
		loadArtists();
		loadTotals();
		loadUnmatched();
		scan.load();
		enrich.load();
		return () => {
			scan.stop();
			enrich.stop();
		};
	});
</script>

<Stats {stats} />

<div class="action-row">
	<Button tone="accent" onclick={() => scan.start()} disabled={scan.running}>{t('action.scan')}</Button>
	<Button tone="accent" onclick={() => enrich.start()} disabled={enrich.running || scan.running}>
		{t('action.enrich')}
	</Button>
	<!-- what the files themselves say, which is a question about the shelf and
	     not about any one record on it -->
	<Button href="/tags">{t('action.tags')}</Button>
</div>

<ScanProgress scan={scan.state} enrich={enrich.state} {scanProgressAt} />

{#if loaded && artists.length === 0}
	<p class="muted">
		{t('music.library.empty')}
		<a href="/search">{t('music.library.emptyCta')}</a>
	</p>
{/if}

<ArtistGrid {artists} />

<UnmatchedFolders
	folders={unmatched}
	scanRunning={scan.running}
	onartistschanged={loadArtists}
	onfolderschanged={loadUnmatched}
/>

<ScanReport scan={scan.state} onretry={refreshAfterRematch} />
