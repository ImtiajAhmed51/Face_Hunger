import { useSyncExternalStore } from 'react';

/**
 * Minimal i18n for new screens: flat keys, `{name}` interpolation, English
 * fallback. Locale is a per-device preference (localStorage), mirrored to
 * <html lang> so screen readers switch voice.
 */
export type Locale = 'en' | 'bn';

const en = {
  'common.all': 'All',
  'common.photos': 'Photos',
  'common.videos': 'Videos',
  'common.loading': 'Loading',
  'common.retry': 'Try again',
  'common.close': 'Close',
  'common.language': 'Language',
  'timeline.eyebrow': 'EVERY DAY, IN ORDER',
  'timeline.title': 'Timeline',
  'timeline.description': 'Your photos and videos by the day they were taken. Drag the scrubber to jump through the years.',
  'timeline.kind': 'Media type',
  'timeline.count': '{count} items',
  'timeline.guessed': '{count} dated from file name or file time',
  'timeline.loading': 'Loading your timeline',
  'timeline.emptyTitle': 'Nothing on the timeline yet',
  'timeline.emptyDescription': 'Add a library in Settings and run a scan. Dates come from the camera, the file name, or the file time.',
  'timeline.scrubber': 'Jump to date',
  'map.eyebrow': 'WHERE IT HAPPENED',
  'map.title': 'Map',
  'map.description': 'Photos and videos with a location, clustered. Select a cluster to see what was taken there.',
  'map.count': '{count} items with a location',
  'map.none': 'No located media',
  'map.noneDescription': 'Photos with GPS in their EXIF data, and videos with a recorded location, appear here once scanned.',
  'map.tilesOff': 'No base map: nothing is downloaded. Add a local PMTiles file in Settings to see streets and coastlines.',
  'map.tilesError': 'Base map unavailable: {error}',
  'map.selection': '{count} items here',
  'map.clear': 'Clear selection',
  'map.canvas': 'Map of your located photos and videos',
  'map.zoomIn': 'Zoom in',
  'map.zoomOut': 'Zoom out',
  'map.fit': 'Show everything',
  'settings.mapTitle': 'Map',
  'settings.mapTiles': 'Show a base map from a local PMTiles file',
  'settings.mapPath': 'PMTiles file on this computer',
  'settings.mapHelp': 'Off by default. No tile server is ever contacted; only this file is read.',
} as const;

export type MessageKey = keyof typeof en;

const bn: Partial<Record<MessageKey, string>> = {
  'common.all': 'সব',
  'common.photos': 'ছবি',
  'common.videos': 'ভিডিও',
  'common.loading': 'লোড হচ্ছে',
  'common.retry': 'আবার চেষ্টা করুন',
  'common.close': 'বন্ধ করুন',
  'common.language': 'ভাষা',
  'timeline.eyebrow': 'প্রতিটি দিন, ক্রমানুসারে',
  'timeline.title': 'টাইমলাইন',
  'timeline.description': 'তোলার দিন অনুযায়ী আপনার ছবি ও ভিডিও। বছরগুলোর মধ্যে যেতে স্ক্রাবার টানুন।',
  'timeline.kind': 'মিডিয়ার ধরন',
  'timeline.count': '{count}টি আইটেম',
  'timeline.guessed': '{count}টির তারিখ ফাইলের নাম বা ফাইলের সময় থেকে',
  'timeline.loading': 'আপনার টাইমলাইন লোড হচ্ছে',
  'timeline.emptyTitle': 'টাইমলাইনে এখনও কিছু নেই',
  'timeline.emptyDescription': 'সেটিংসে একটি লাইব্রেরি যোগ করে স্ক্যান চালান। তারিখ আসে ক্যামেরা, ফাইলের নাম বা ফাইলের সময় থেকে।',
  'timeline.scrubber': 'তারিখে যান',
  'map.eyebrow': 'যেখানে ঘটেছিল',
  'map.title': 'মানচিত্র',
  'map.description': 'অবস্থানসহ ছবি ও ভিডিও, গুচ্ছ আকারে। কোথায় কী তোলা হয়েছে দেখতে একটি গুচ্ছ বেছে নিন।',
  'map.count': 'অবস্থানসহ {count}টি আইটেম',
  'map.none': 'অবস্থানসহ কোনো মিডিয়া নেই',
  'map.noneDescription': 'EXIF-এ GPS থাকা ছবি এবং অবস্থান রেকর্ড করা ভিডিও স্ক্যানের পরে এখানে দেখা যাবে।',
  'map.tilesOff': 'কোনো বেস ম্যাপ নেই: কিছুই ডাউনলোড হয় না। রাস্তা ও উপকূল দেখতে সেটিংসে একটি লোকাল PMTiles ফাইল যোগ করুন।',
  'map.tilesError': 'বেস ম্যাপ পাওয়া যাচ্ছে না: {error}',
  'map.selection': 'এখানে {count}টি আইটেম',
  'map.clear': 'নির্বাচন মুছুন',
  'map.canvas': 'আপনার অবস্থানসহ ছবি ও ভিডিওর মানচিত্র',
  'map.zoomIn': 'বড় করুন',
  'map.zoomOut': 'ছোট করুন',
  'map.fit': 'সব দেখান',
  'settings.mapTitle': 'মানচিত্র',
  'settings.mapTiles': 'লোকাল PMTiles ফাইল থেকে বেস ম্যাপ দেখান',
  'settings.mapPath': 'এই কম্পিউটারে PMTiles ফাইল',
  'settings.mapHelp': 'ডিফল্টভাবে বন্ধ। কোনো টাইল সার্ভারের সাথে যোগাযোগ করা হয় না; শুধু এই ফাইলটি পড়া হয়।',
};

export const MESSAGES: Record<Locale, Partial<Record<MessageKey, string>>> = { en, bn };
export const LOCALES: { value: Locale; label: string }[] = [{ value: 'en', label: 'English' }, { value: 'bn', label: 'বাংলা' }];

let locale: Locale = (() => {
  try {
    const saved = localStorage.getItem('lfs-lang');
    return saved === 'bn' ? 'bn' : 'en';
  } catch {
    return 'en';
  }
})();
const listeners = new Set<() => void>();
if (typeof document !== 'undefined') document.documentElement.lang = locale;

export function getLocale(): Locale {
  return locale;
}

export function setLocale(next: Locale): void {
  locale = next;
  try { localStorage.setItem('lfs-lang', next); } catch { /* optional */ }
  if (typeof document !== 'undefined') document.documentElement.lang = next;
  listeners.forEach((listener) => listener());
}

export function translate(key: MessageKey, vars?: Record<string, string | number>, lang: Locale = locale): string {
  const template = MESSAGES[lang][key] ?? en[key] ?? key;
  return vars ? template.replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? `{${name}}`)) : template;
}

export function useT() {
  const current = useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    () => locale,
    () => locale,
  );
  return (key: MessageKey, vars?: Record<string, string | number>) => translate(key, vars, current);
}
