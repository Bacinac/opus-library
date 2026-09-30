<script lang="ts">
	// Every song and everything its file says, in one table.
	//
	// The tags were readable a row at a time on the page about one record, which
	// answers a question about that record and not the one somebody actually
	// has: what is in my files, and where do they disagree with each other.
	//
	// Which columns are shown is a choice, because forty-one of them is not a
	// table anybody can read — the common ones stand by default and the rest are
	// there to be turned on.

	import { onMount } from 'svelte';
	import { Button, Latest, PageHead, Picks, SearchBox, formatNumber, plural, request } from '$lib/kit';
	import { t } from '$lib/i18n';

	type Row = {
		id: number;
		path: string;
		artist: string | null;
		album: string | null;
		title: string | null;
		track: number | null;
		tags: Record<string, string | string[]>;
	};
	type Answer = { total: number; keys: { key: string; files: number }[]; rows: Row[] };

	const SHOWN = ['ARTIST', 'ALBUM', 'TITLE', 'TRACKNUMBER', 'DATE', 'GENRE'];
	const PAGE = 100;

	let rows = $state<Row[]>([]);
	let keys = $state<{ key: string; files: number }[]>([]);
	let chosen = $state<string[]>([...SHOWN]);
	let total = $state(0);
	let offset = $state(0);
	let q = $state('');
	let loading = $state(false);
	let picking = $state(false);
	const asking = new Latest();

	async function load() {
		loading = true;
		const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
		if (q) params.set('q', q);
		const got = await request<Answer>(`/api/music/library/tags?${params}`, {}, { latest: asking });
		loading = false;
		if (!got) return;
		rows = got.rows;
		keys = got.keys;
		total = got.total;
	}

	function look() {
		offset = 0;
		load();
	}

	function page(by: number) {
		offset = Math.max(0, offset + by);
		load();
	}

	function say(value: string | string[] | undefined): string {
		if (value === undefined) return '';
		return Array.isArray(value) ? value.join(' · ') : value;
	}

	onMount(load);
</script>

<PageHead
	sticky={false}
	facts={[{ text: plural(total, 'tags.files.one', 'tags.files.few', 'tags.files.many') }]}
/>

<SearchBox bind:value={q} placeholder={t('tags.search')} action={t('tags.look')} tone="accent" onsubmit={look}>
	{#snippet after()}
		<Button selected={picking} onclick={() => (picking = !picking)}>
			{t('tags.columns', { n: formatNumber(chosen.length) })}
		</Button>
	{/snippet}
</SearchBox>

<!-- the columns on offer, each with how many files carry it: a tag two files
     have is not a column, and knowing that before turning it on is the point.
     Shut, because a hundred of these between the search box and the first song
     is a wall in front of the thing the page is for. -->
{#if picking}
	<div class="columns">
		<Picks
			picks={keys.map((k) => ({ key: k.key, label: k.key, note: formatNumber(k.files) }))}
			bind:chosen
			many
		/>
	</div>
{/if}

{#if loading}
	<p class="muted">{t('common.loading')}</p>
{:else if rows.length === 0}
	<p class="muted">{t('tags.none')}</p>
{:else}
	<div class="table-scroll">
		<table>
			<thead>
				<tr>
					{#each chosen as key (key)}<th>{key}</th>{/each}
					<th>{t('tags.file')}</th>
				</tr>
			</thead>
			<tbody>
				{#each rows as row (row.id)}
					<tr>
						{#each chosen as key (key)}<td title={say(row.tags[key])}>{say(row.tags[key])}</td>{/each}
						<td class="path" title={row.path}>{row.path.split('/').pop()}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	<div class="pager">
		<Button onclick={() => page(-PAGE)} disabled={offset === 0}>{t('tags.back')}</Button>
		<span class="muted">{formatNumber(offset + 1)}–{formatNumber(offset + rows.length)}</span>
		<Button onclick={() => page(PAGE)} disabled={offset + rows.length >= total}>
			{t('tags.next')}
		</Button>
	</div>
{/if}

<style>
	.columns {
		margin-bottom: 0.9rem;
		font-size: var(--fs-s);
		font-family: ui-monospace, monospace;
	}
	table {
		font-size: var(--fs-s);
	}
	th,
	td {
		padding: 0.25rem 0.6rem;
		max-width: 22rem;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	th {
		color: var(--muted);
		font-family: ui-monospace, monospace;
		font-weight: 600;
	}
	.path {
		color: var(--muted);
	}
	.pager {
		display: flex;
		align-items: center;
		gap: 0.8rem;
		margin: 0.9rem 0;
	}
</style>
