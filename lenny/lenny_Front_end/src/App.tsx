import './App.css'

function App() {
  return (
    <>
      <h1 className="text-3xl flex justify-center p-10 font-bold ">
      Lenny
      </h1>
      <div className="flex justify-center ">
      <img src="./src/assets/lenny.png"  alt="Lenny" />
      </div>
      <br/>
      <p className='flex justify-center text-xl font-normal'>Lenny is a free, open source Library Lending System. You can learn more about it on&nbsp;
        <a href="https://github.com/ArchiveLabs/lenny" className='underline'>github</a>.
      </p>
    
    </>
  )
}

export default App
