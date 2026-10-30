/** A picture the library holds, at the width a screen draws it in device
 *  pixels. The library keeps a handful of widths and sends the smallest one
 *  that covers this. */
export function picture(address: string | null | undefined, width: number): string | null {
	return address ? `${address}?w=${width}` : null;
}
