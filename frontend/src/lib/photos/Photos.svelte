<script lang="ts">
	// The photo half has two ways in, and they are not two views of one thing.
	// The timeline is a shelf in the order it happened; people is who is on it,
	// each one assembled across the years. Neither reskins the other.

	import { SvelteSet } from 'svelte/reactivity';
	import { Button, PageHead, Picks, SearchBox, formatNumber, request } from '$lib/kit';
	import { me } from '$lib/opus';
	import PhotoQuery from '$lib/opus/PhotoQuery.svelte';
	import PhotoTimeline from '$lib/opus/PhotoTimeline.svelte';
	import { t, type MessageKey } from '$lib/i18n';
	import Group from './Group.svelte';
	import Nameless from './Nameless.svelte';
	import People from './People.svelte';
	import Places from './Places.svelte';
	import ShareLink from './ShareLink.svelte';
	import Shares from './Shares.svelte';

	// the passes the library runs over itself, in the order it runs them
	const PASSES = {
		scanning: 'photos.busy.scanning',
		metadata: 'photos.busy.metadata',
		derivatives: 'photos.busy.derivatives',
		faces: 'photos.busy.faces',
		recordings: 'photos.busy.recordings',
		grouping: 'photos.busy.grouping',
		ages: 'photos.busy.ages',
		recognising: 'photos.busy.recognising',
		places: 'photos.busy.places'
	} as const satisfies Record<string, MessageKey>;
	type Pass = keyof typeof PASSES;

	// What the library is doing to itself, said only while it is doing it. Green
	// passes and finished work are not news; a step that refused is.
	let doing = $state<Pass | ''>('');
	let trouble = $state('');
	$effect(() => {
		if (!me.admin) return;
		const ask = async () => {
			const said = await request<{ doing: Pass | ''; trouble: string }>('/api/photos/library/keeping-up');
			if (!said) return;
			doing = said.doing;
			trouble = said.trouble;
		};
		ask();
		const tick = setInterval(ask, 20000);
		return () => clearInterval(tick);
	});

	// the library writes a refusal as "<pass>: <why>"
	let stalled = $derived.by(() => {
		const at = trouble.indexOf(': ');
		const pass = trouble.slice(0, at) as Pass;
		return at > 0 && pass in PASSES
			? t('photos.stalled', { pass: t(PASSES[pass]), detail: trouble.slice(at + 2) })
			: trouble;
	});

	type View = 'timeline' | 'places' | 'people' | 'nameless' | 'shared';
	let view = $state<View>('timeline');
	let here = $state<string | null>(null);
	let typed = $state('');
	let search = $state('');
	let open = $state<number | null>(null);
	let stamp = $state(0);
	let chosen = $state<SvelteSet<string> | null>(null);
	let sharing = $state(false);
	const views = $derived([
		{ key: 'timeline', label: t('photos.view.timeline') },
		{ key: 'places', label: t('photos.view.places') },
		{ key: 'people', label: t('photos.view.people') },
		// last, because it is a question about the people view rather than a
		// fourth way into the photographs: who the machine has gathered and
		// nobody has yet said a word about
		{ key: 'nameless', label: t('nameless.title') },
		...(me.admin ? [{ key: 'shared', label: t('photos.view.shared') }] : [])
	]);
</script>

<PageHead>
	{#snippet ways()}
		<Picks
			picks={views}
			chosen={[view]}
			onpick={(key) => {
				view = key as View;
				here = null;
				chosen = null;
			}}
		/>
		{#if doing || trouble}
			<p class="doing" class:warn={!!trouble && !doing} title={doing ? '' : stalled}>
				{#if doing}
					<span class="spin"></span>
					{t(PASSES[doing])}
				{:else}
					{stalled}
				{/if}
			</p>
		{/if}
	{/snippet}
</PageHead>

{#if view === 'timeline'}
	<SearchBox
		bind:value={typed}
		placeholder={t('photos.search.hint')}
		action={t('common.search')}
		onsubmit={() => (search = typed.trim())}
	>
		{#snippet after()}
			{#if me.admin && chosen}
				<span class="chosen">{t('photos.share.chosen', { n: formatNumber(chosen.size) })}</span>
				<Button tone="accent" disabled={!chosen.size} onclick={() => (sharing = true)}>
					{t('photos.share.make')}
				</Button>
				<Button onclick={() => (chosen = null)}>{t('photos.share.stop')}</Button>
			{:else if me.admin}
				<Button onclick={() => (chosen = new SvelteSet())}>{t('photos.share.choose')}</Button>
			{/if}
		{/snippet}
	</SearchBox>
	{#if search}
		<PhotoQuery {search} />
	{/if}
	{#key search}
		<PhotoTimeline
			{search}
			canEdit={me.admin}
			canDelete={me.admin}
			chosen={chosen ?? undefined}
			choosable={(p) => p.kind === 'image' && p.ready}
		/>
	{/key}
{:else if view === 'places'}
	{#if here}
		<p class="back">
			<Button size="small" onclick={() => (here = null)}>← {t('photos.view.places')}</Button>
			<strong>{here}</strong>
		</p>
		{#key here}
			<PhotoTimeline place={here} canEdit={me.admin} canDelete={me.admin} />
		{/key}
	{:else}
		<Places onopen={(p) => (here = p)} />
	{/if}
{:else if view === 'people'}
	{#key stamp}
		<People onopen={(id) => (open = id)} />
	{/key}
{:else if view === 'shared'}
	<Shares />
{:else}
	{#key stamp}
		<Nameless onopen={(id) => (open = id)} />
	{/key}
{/if}

{#if sharing && chosen}
	<ShareLink
		photos={[...chosen]}
		onclose={(made) => {
			sharing = false;
			if (made) chosen = null;
		}}
	/>
{/if}

{#if open !== null}
	<Group cluster={open} onclose={() => (open = null)} onchanged={() => (stamp += 1)} />
{/if}

<style>
	.chosen {
		align-self: center;
		color: var(--muted);
		font-size: var(--fs-m);
	}
	/* on the line of the title, where a pass starting and stopping moves
	   nothing under it */
	.doing {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		max-width: min(40rem, 60vw);
		margin: 0 0 0 auto;
		font-size: var(--fs-s);
		color: var(--muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.doing.warn {
		color: var(--warn);
	}
	.spin {
		width: 0.6rem;
		height: 0.6rem;
		border-radius: 50%;
		border: 2px solid var(--accent);
		border-top-color: transparent;
		animation: turn 0.9s linear infinite;
	}
	@keyframes turn {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin {
			animation: none;
		}
	}
	.back {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		margin: 0 0 0.8rem;
	}
</style>
