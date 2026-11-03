# Referência visual: auxia.io

Escolhida pelo dono do projeto em 03/11/2026 (via https://godly.design/website/auxia/).
Print da home: `auxia-home.png`. Usamos o **estilo** (cores, tipografia, formas), não a marca,
os textos nem as ilustrações deles.

## O que caracteriza o site
- Fundo claro cor de aveia/creme, não branco. Seções alternam com blocos grafite escuros.
- Um único azul forte para ação (botões, linhas, destaques); laranja como cor secundária rara.
- Títulos enormes, peso médio (500), letras bem apertadas (letter-spacing ≈ -3%).
- Etiquetas pequenas em fonte **mono maiúscula** (IBM Plex Mono), muitas vezes em "pílulas"
  com um ícone de raio — dão o ar de "sistema/agente trabalhando".
- Cartões creme quase brancos, borda fina translúcida e sombra longa e suave em camadas.
- Botões com cantos de 12px; pílulas totalmente arredondadas.
- Bastante respiro; conteúdo em até ~1232px.

## Tokens extraídos (valores reais do CSS deles)
| Uso | Valor |
|---|---|
| Fundo da página | `#f0efe3` |
| Cartão | `#fefdf5` |
| Fundo alternativo | `#e2e1d3` |
| Borda / texto apagado | `#c3c2b2`, `rgba(35,35,35,.1)` |
| Texto / seções escuras | `#232323` |
| Azul de ação | `#0b4fff` (suave: `rgba(11,79,255,.2)`) |
| Laranja secundário | `#fa6838` |
| Sucesso | fundo `#cef5ca`, texto `#114e0b` |
| Aviso | fundo `#fcf8d8`, texto `#5e5515` |
| Erro | fundo `#f8e4e4`, texto `#3b0b0b` |

| Tipografia | Valor |
|---|---|
| Texto e títulos | PP Neue Montreal 500 (fonte paga) → usamos **Inter Tight** (Google Fonts), parecida |
| Etiquetas | IBM Plex Mono 500, maiúsculas, 11–14px |
| H1 | 104px / 0.95, -3% |
| H2 | 64px / 0.95, -3% |
| Corpo | 16px / 1.3 |

| Forma | Valor |
|---|---|
| Botão | raio 12px, padding 14px 24px (pequeno: 8px 20px) |
| Cartão | raio 12–24px, borda `1px rgba(35,35,35,.1)` |
| Pílula | raio 999px, padding 4px 8px |
| Sombra | `0 12px 27px rgba(0,0,0,.18), 0 49px 49px rgba(0,0,0,.14), 0 111px 67px rgba(0,0,0,.1)` (usamos uma versão mais leve) |

## Como aplicamos no painel
- Mesmo fundo aveia, cartões creme, texto grafite, azul só para ação principal.
- Títulos de página grandes e apertados; números do painel no estilo "200 billion".
- Etiquetas mono maiúsculas para categoria, status, datas e rótulos de campos.
- Interruptor "Respostas automáticas" em destaque no topo; azul quando ligado.
- Cores de sucesso/aviso/erro deles para as categorias (elogio, atraso, defeito...).
