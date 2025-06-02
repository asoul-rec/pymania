def parse_osu_beatmap(beatmap_path: str) -> dict:
    """
    Parse an osu! beatmap file and return its metadata.

    :param beatmap_path: Path to the osu! beatmap file.
    :return: A dictionary containing the beatmap metadata.
    """
    LIST_SECTIONS = {'Events', 'TimingPoints', 'HitObjects'}
    KV_SECTIONS = {
        'General': {
            '_split':                   ': ',
            'AudioFilename':            str,
            'AudioLeadIn':              int,
            'AudioHash':                str,
            'PreviewTime':              int,
            'Countdown':                int,
            'SampleSet':                str,
            'StackLeniency':            float,
            'Mode':                     int,
            'LetterboxInBreaks':        bool,
            'StoryFireInFront':         bool,
            'UseSkinSprites':           bool,
            'AlwaysShowPlayfield':      bool,
            'OverlayPosition':          str,
            'SkinPreference':           str,
            'EpilepsyWarning':          bool,
            'CountdownOffset':          int,
            'SpecialStyle':             bool,
            'WidescreenStoryboard':     bool,
            'SamplesMatchPlaybackRate': bool,
        },
        'Editor': {
            '_split':                   ': ',
            'Bookmarks':                lambda x: [int(i) for i in x.split(',')],
            'DistanceSpacing':          float,
            'BeatDivisor':              int,
            'GridSize':                 int,
            'TimelineZoom':             float,
        },
        'Metadata': {
            '_split':                   ':',
            'Title':                    str,
            'TitleUnicode':             str,
            'Artist':                   str,
            'ArtistUnicode':            str,
            'Creator':                  str,
            'Version':                  str,
            'Source':                   str,
            'Tags':                     lambda x: [i.strip() for i in x.split(' ') if i],
            'BeatmapID':                int,
            'BeatmapSetID':             int,
        },
        'Difficulty': {
            '_split':                   ':',
            'HPDrainRate':              float,
            'CircleSize':               float,
            'OverallDifficulty':        float,
            'ApproachRate':             float,
            'SliderMultiplier':         float,
            'SliderTickRate':           float,
        },
        'Colours': {
            '_split':                   ' : ',
        }
    }
    data = {}
    with open(beatmap_path, 'r', encoding='utf-8') as f:
        section = "Version"
        content = []

        for line in f:
            if not line.strip():
                continue
            if line.startswith('[') and line.endswith(']\n'):
                data[section] = content
                section = line[1:-2]
                if section in KV_SECTIONS:
                    content = {}
                elif section in LIST_SECTIONS:
                    content = []
                else:
                    raise ValueError(f"Unknown section: {section}")
            else:
                line = line[:-1]  # Remove the trailing newline character
                if section == 'Version':
                    content.append(line)
                if section in KV_SECTIONS:
                    section_format = KV_SECTIONS[section]
                    key, value = line.split(section_format['_split'], maxsplit=1)
                    content[key] = section_format[key](value) if key in section_format else value
                elif section in LIST_SECTIONS:
                    content.append(line.split(','))
        data[section] = content  # Add the last section
    return data
