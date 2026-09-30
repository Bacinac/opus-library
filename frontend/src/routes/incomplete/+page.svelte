<script lang="ts">
	// The albums the shelf counts as incomplete, named. The number on the shelf
	// says how many; this says which, whose they are, and how much of each is
	// missing — so the answer to a doubtful album can be that one album rather
	// than the button that wipes every one of them.

	import { onMount } from 'svelte';
	import { t } from '$lib/i18n';
	import { ArmedButton, PageHead, formatNumber, plural, request, toasts } from '$lib/kit';

	type Row = {
		release_id: number;
		title: string;
		year: string | null;
		artist_id: number;
		artist: string;
		have: number;
		need: number;
		origins: number;
	};

	let rows = $state<Row[]>([]);
	let loaded = $state(false);

	onMount(async () => {
		const got = await request<Row[]>('/api/music/library/incomplete');
		if (got) rows = got;
		loaded = true;
	});

	// Which albums are already on their way. Per album and not a page-wide mode:
	// the whole reason this is here rather than on the shelf is that the answer
	// differs album by album.
	let sent = $state<number[]>([]);

	async function redownload(row: Row) {
		const queued = await request(`/api/music/library/incomplete/${row.release_id}/redownload`, {
			method: 'POST'
		});
		if (!queued) return;
		sent = [...sent, row.release_id];
		toasts.success(t('incomplete.queued', { title: row.title }));
	}
</script>

<PageHead
	sticky={false}
	facts={loaded && rows.length
		? [{ text: plural(rows.length, 'incomplete.albums.one', 'incomplete.albums.few', 'incomplete.albums.many'), tone: 'warn' }]
		: []}
/>

{#if !loaded}
	<p class="muted">{t('common.loading')}</p>
{:else if rows.length === 0}
	<p class="muted">{t('incomplete.none')}</p>
{:else}
	<ul class="albums">
		{#each rows as row (row.release_id)}
			<li>
				<a class="who" href="/artists/{row.artist_id}">{row.artist}</a>
				<span class="what">
					{row.title}{#if row.year}<span class="muted">&nbsp;({row.year})</span>{/if}
				</span>
				<span class="held" class:stitched={row.origins > 1}>
					{#if row.have < row.need}
						{t('incomplete.held', { have: formatNumber(row.have), need: formatNumber(row.need) })}
					{:else}
						{t('incomplete.stitched', { n: formatNumber(row.origins) })}
					{/if}
				</span>
				{#if sent.includes(row.release_id)}
					<span class="onway">{t('incomplete.onway')}</span>
				{:else}
					<ArmedButton tone="quiet" onconfirm={() => redownload(row)}>{t('incomplete.redownload')}</ArmedButton>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.albums {
		margin: 0.8rem 0;
		padding: 0;
		list-style: none;
	}
	.albums li {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0.15rem 0.9rem;
		padding: 0.45rem 0;
		border-bottom: 1px solid var(--border);
	}
	.who {
		min-width: 12rem;
		color: var(--muted);
		text-decoration: none;
	}
	.who:hover {
		color: var(--accent);
		text-decoration: underline;
	}
	.what {
		flex: 1;
	}
	.held {
		color: var(--warn);
		font-size: var(--fs-m);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.held.stitched {
		color: var(--muted);
	}
	.onway {
		color: var(--accent);
		font-size: var(--fs-m);
		white-space: nowrap;
	}
</style>
