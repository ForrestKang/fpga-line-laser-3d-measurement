"""R42 buttons: manifest-gated reads, modulo counters and direct page selection."""
MAGIC = 0x52343242
BASE = 0x600


class Buttons:
    def __init__(self, manifest, mmio_factory, phys_addr):
        block = manifest.get('buttons') or {}
        if block.get('present') is not True:
            raise ValueError('manifest does not declare R42 buttons; refusing MMIO probe')
        if block.get('register_window') != [0x600, 0x6ff] or block.get('magic') != MAGIC:
            raise ValueError('invalid R42 button register contract')
        self.mmio = mmio_factory(int(phys_addr), 4096)
        if int(self.mmio.read(BASE)) != MAGIC:
            raise RuntimeError('R42 button magic mismatch')
        self.previous = self._counts()
        self.level = 0

    def _counts(self):
        value = int(self.mmio.read(BASE + 8))
        return tuple((value >> (8 * index)) & 255 for index in range(4))

    def poll(self):
        self.level = int(self.mmio.read(BASE + 4)) & 15
        current = self._counts()
        delta = tuple((new - old) & 255 for new, old in zip(current, self.previous))
        self.previous = current
        return delta


class Pages:
    """BTN1..3 select a page, never cycle; one configured page disables switching."""
    def __init__(self, pages=('A', 'B', 'C'), mapping=('A', 'B', 'C'), initial='A'):
        self.pages = tuple(pages)
        self.mapping = tuple(mapping)
        if (not self.pages or len(set(self.pages)) != len(self.pages)
                or any(page not in ('A', 'B', 'C') for page in self.pages)
                or len(self.mapping) != 3 or any(page not in ('A', 'B', 'C') for page in self.mapping)
                or initial not in self.pages):
            raise ValueError('pages/mapping must use A,B,C and initial must be enabled')
        self.current = initial

    def poll(self, delta):
        before = self.current
        if len(self.pages) > 1:
            for button, page in enumerate(self.mapping, 1):
                if delta[button] and page in self.pages:
                    self.current = page
        return before != self.current
