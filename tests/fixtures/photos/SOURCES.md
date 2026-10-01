# Photo fixtures — sources and licences

Three real, permissively-licensed photographs used as the §9 detector positive
controls (10-slack-io.md §8). Each is downscaled so its longer side is at most
1024 px and kept under 400 KB. Expected `count_faces` at the default
`faces.score_threshold` (`"0.9"`) is noted per file and asserted by the slow
real-detector test in `tests/test_faces.py`.

| File | Faces | Licence |
|---|---|---|
| `selfie_two_faces.jpg` | 2 | Public domain |
| `portrait_one_face.jpg` | 1 | CC0 1.0 (public-domain dedication) |
| `landscape_no_face.jpg` | 0 | Public domain |

## selfie_two_faces.jpg (2 faces)

- Title: *Couple, assis, à mi-genoux, de face* (btv1b6902897j, 3 of 3)
- Source: https://commons.wikimedia.org/wiki/File:Couple,_assis,_%C3%A0_mi-genoux,_de_face_-_btv1b6902897j_(3_of_3).jpg
- Author: F. Moissenet (photographer, 19th century)
- Licence: Public domain (published before 1900; author died over 70 years ago)

## portrait_one_face.jpg (1 face)

- Title: *Elderly Man; Full Face* (The Metropolitan Museum of Art, 37.14.52)
- Source: https://commons.wikimedia.org/wiki/File:-Elderly_Man;_Full_Face-_MET_37.14.52.jpg
- Author: Albert Sands Southworth (daguerreotype; Southworth & Hawes)
- Licence: CC0 1.0 (The Met open-access public-domain dedication)

## landscape_no_face.jpg (0 faces)

- Title: *Field, corn, Liechtenstein, Mountains, Alps, Vaduz, sky, clouds, landscape*
- Source: https://commons.wikimedia.org/wiki/File:Field,_corn,_Liechtenstein,_Mountains,_Alps,_Vaduz,_sky,_clouds,_landscape.jpg
- Author: Wikimedia Commons user "Paranoid"
- Licence: Public domain (released by the author)

These are purpose-selected test images, not captured Slack payloads, so no scrub
map applies (10-slack-io.md §8). They are the only binary fixtures in the suite.
