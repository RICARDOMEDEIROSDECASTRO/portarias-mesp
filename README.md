# Portarias do Ministério do Esporte — atualização automática via DOU

Este pacote contém uma versão estática do site e uma rotina opcional de atualização automática.

## Como funciona

1. O site fica hospedado no Netlify.
2. O código e o `index.html` ficam em um repositório GitHub.
3. O GitHub Actions executa diariamente o script `scripts/atualizar_dou.py`.
4. O script consulta o DOU, procura novas ocorrências de `portaria MESP`, remove duplicidades e atualiza o bloco `const DATA = [...]` dentro do `index.html`.
5. Havendo novas portarias, o próprio workflow faz commit no repositório.
6. Se o Netlify estiver conectado ao GitHub, ele republica automaticamente o site.

## Primeiro uso

1. Crie um repositório no GitHub.
2. Envie todos os arquivos desta pasta para o repositório.
3. No GitHub, vá em `Actions` e habilite workflows, se solicitado.
4. Em Netlify, conecte o site a esse repositório GitHub.
5. Configure o Netlify para publicar a raiz do projeto. Não há build command.
6. Para testar, vá no GitHub em `Actions > Atualizar portarias do DOU > Run workflow`.

## Horário

O workflow está configurado para rodar todos os dias às 7h no fuso `America/Sao_Paulo`.

Para mudar o horário, edite `.github/workflows/atualizar-dou.yml`.

## Observação

O raspador usa a busca pública do DOU. Se o layout ou os parâmetros da página do DOU mudarem, pode ser necessário ajustar o script.
