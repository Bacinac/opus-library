<script lang="ts">
	// An account with another service, linked by approving a code on that
	// service's own page rather than by typing a password here.

	import { onDestroy } from 'svelte';
	import { t, type MessageKey } from '$lib/i18n';
	import { ArmedButton, Button, Card, request, toasts } from '$lib/kit';
	import type { Link } from '$lib/core/types';

	let { link, onchanged }: { link: Link; onchanged: (link: Link) => void } = $props();

	let service = $derived(t(`service.${link.name}` as MessageKey));
	let approvalUrl = $state<string | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | undefined;

	function stopPolling() {
		clearInterval(pollTimer);
		pollTimer = undefined;
		approvalUrl = null;
	}

	function startPolling() {
		clearInterval(pollTimer);
		pollTimer = setInterval(async () => {
			const now = (await request<Link[]>('/api/links'))?.find((l) => l.name === link.name);
			if (!now) {
				stopPolling();
				return;
			}
			onchanged(now);
			if (now.pending) return;
			stopPolling();
			if (now.error) toasts.error(t('settings.link.error', { detail: now.error }));
			else toasts.success(t('settings.link.linked', { service }));
		}, 2000);
	}

	async function connect() {
		const said = await request<{ url: string }>(`/api/links/${link.name}`, { method: 'POST' });
		if (!said) return;
		approvalUrl = said.url;
		window.open(said.url, '_blank', 'noopener');
		startPolling();
	}

	onDestroy(stopPolling);

	async function unlink(id: number) {
		if (!(await request(`/api/links/${link.name}/${id}`, { method: 'DELETE' }))) return;
		toasts.success(t('settings.link.removed', { service }));
		const now = (await request<Link[]>('/api/links'))?.find((l) => l.name === link.name);
		if (now) onchanged(now);
	}
</script>

<Card title={t('settings.link.title', { service })} collapsible>
	{#if link.accounts.length > 0}
		<ul class="accounts">
			{#each link.accounts as account (account.id)}
				<li>
					<span class="label">{account.label}</span>
					<ArmedButton onconfirm={() => unlink(account.id)}>{t('settings.link.remove')}</ArmedButton>
				</li>
			{/each}
		</ul>
	{/if}
	{#if approvalUrl}
		<p class="waiting">
			<a href={approvalUrl} target="_blank" rel="noreferrer">{approvalUrl}</a>
		</p>
	{:else}
		<div>
			<Button onclick={connect}>{t('settings.link.connect', { service })}</Button>
		</div>
	{/if}
</Card>

<style>
	.waiting {
		margin: 0;
		overflow-wrap: anywhere;
	}
	.accounts {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
	}
	.accounts li {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.45rem 0;
		border-bottom: 1px solid var(--border);
	}
	.accounts li:last-child {
		border-bottom: none;
	}
	.label {
		overflow-wrap: anywhere;
	}
</style>
