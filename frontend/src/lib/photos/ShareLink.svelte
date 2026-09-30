<script lang="ts">
	// The chosen photographs made into a link for somebody outside the house.
	// The library keeps only a digest of the key, so the address exists on this
	// screen and nowhere else: it is handed on from here or not at all.

	import { Button, Dialog, Picks, json, plural, request, toasts } from '$lib/kit';
	import { t } from '$lib/i18n';

	let {
		photos,
		onclose
	}: {
		photos: string[];
		/** told whether a link was made, so an unmade one keeps the choice */
		onclose: (made: boolean) => void;
	} = $props();

	const LASTS = [1, 7, 30, 90];
	let days = $state('7');
	let link = $state('');
	let making = $state(false);

	async function make() {
		making = true;
		const made = await request<{ key: string }>(
			'/api/photos/shares',
			json({ photos, days: Number(days) })
		);
		making = false;
		if (made) link = `${location.origin}/s/${made.key}`;
	}

	async function copy() {
		await navigator.clipboard.writeText(link);
		toasts.success(t('photos.share.copied'));
	}
</script>

<Dialog
	title={t('photos.share.title')}
	subtitle={plural(photos.length, 'photos.count.one', 'photos.count.few', 'photos.count.many')}
	size="narrow"
	onclose={() => onclose(link !== '')}
>
	<div class="sheet">
		<p class="what">{t('photos.share.what')}</p>
		{#if link}
			<code>{link}</code>
			<p class="once">{t('photos.share.once')}</p>
			<div class="acts">
				{#if navigator.clipboard}
					<Button tone="primary" onclick={copy}>{t('photos.share.copy')}</Button>
				{/if}
				{#if navigator.share}
					<Button onclick={() => navigator.share({ url: link }).catch(() => {})}>
						{t('photos.share.send')}
					</Button>
				{/if}
			</div>
		{:else}
			<p class="lasts">{t('photos.share.lasts')}</p>
			<Picks
				picks={LASTS.map((n) => ({
					key: String(n),
					label: plural(n, 'photos.share.days.one', 'photos.share.days.few', 'photos.share.days.many')
				}))}
				chosen={[days]}
				onpick={(key) => (days = key)}
			/>
			<div class="acts">
				<Button tone="primary" disabled={making} onclick={make}>{t('photos.share.create')}</Button>
			</div>
		{/if}
	</div>
</Dialog>

<style>
	.sheet {
		display: grid;
		gap: 0.8rem;
	}
	.what,
	.once,
	.lasts {
		margin: 0;
		font-size: var(--fs-m);
	}
	.what,
	.once {
		color: var(--muted);
	}
	code {
		padding: 0.45rem 0.7rem;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--surface);
		font-size: var(--fs-s);
		word-break: break-all;
		user-select: all;
	}
	.acts {
		display: flex;
		gap: 0.5rem;
	}
</style>
